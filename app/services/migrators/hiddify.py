"""Hiddify Manager → PasarGuard migration (users + subscription redirect).

Flow (same idea as 3x-ui):
1. Read users + old subscription paths from Hiddify JSON export
2. Create PasarGuard group ``hiddify-test``
3. Create users (preserve UUID) into that group
4. Install pg-redirect so old Hiddify links → new /sub/{token}

Inbounds / proxy templates are intentionally NOT migrated.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.config import (
    BACKUP_DIR,
    HIDDIFY_DIR,
    HIDDIFY_MYSQL_PASS,
    PASARGUARD_DATA,
    PASARGUARD_DIR,
    PASARGUARD_ENV,
)
from app.services.migrators.base import BaseMigrator
from app.services.migrators.hiddify_lib import (
    build_subscription_mapping,
    extract_listen_ports,
    extract_subscription_domains,
    find_hiddify_json_in_dir,
    load_hiddify_json_file,
    parse_users_from_backup,
    summarize_backup,
)
from app.services.migrators.hiddify_pg_import import (
    HIDDIFY_TEST_GROUP,
    run_hiddify_user_import,
)
from app.services.pasarguard_ops import safe_start_pasarguard
from app.services.pg_access import resolve_pasarguard_public_base


class HiddifyMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        install_redirect = params.get("install_redirect", True)
        upload_path = params.get("upload_path")
        upload_work_dir = params.get("upload_work_dir")

        self.job.set_progress(5, "بررسی پیش‌نیازها...")
        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard باید قبل از مهاجرت نصب باشد")

        work = BACKUP_DIR / self.job.job_id
        work.mkdir(parents=True, exist_ok=True)

        self.job.set_progress(15, "خواندن کاربران و لینک‌ها از JSON هیدیفای...")
        data = await self._load_backup(upload_path, upload_work_dir, params)
        summary = summarize_backup(data)
        users, paths = parse_users_from_backup(data)
        if not users:
            raise RuntimeError("هیچ کاربر معتبری در بکاپ Hiddify یافت نشد")

        client_path = paths.get("proxy_path_client") or ""
        root_path = paths.get("proxy_path") or ""
        sub_domains = extract_subscription_domains(data.get("domains") or [])
        listen_ports = extract_listen_ports(data.get("hconfigs") or [])
        if not client_path:
            self.job.log("هشدار: proxy_path_client در بکاپ خالی است — ریدایرکت ممکن است ناقص باشد")
        if sub_domains:
            self.job.log(f"Hiddify subscription hosts: {', '.join(sub_domains[:8])}")
        self.job.log(
            f"Hiddify JSON: {summary['users_total']} users "
            f"(enabled={summary['users_enabled']}), "
            f"proxy_path_client={client_path!r}, "
            f"https_ports={listen_ports.get('https')}"
        )

        self.job.set_progress(
            30,
            f"ساخت گروه {HIDDIFY_TEST_GROUP} و ایجاد کاربران در PasarGuard...",
        )
        import_result = await run_hiddify_user_import(self, users, work)
        created = import_result.get("created") or []
        errors = import_result.get("errors") or []
        if not created:
            detail = (
                (errors[0].get("error") if errors else None)
                or import_result.get("error")
                or import_result.get("traceback")
                or "unknown"
            )
            detail = str(detail).strip()
            if len(detail) > 800:
                detail = detail[:400] + "\n…\n" + detail[-400:]
            raise RuntimeError(f"ایجاد کاربران در PasarGuard ناموفق بود: {detail}")

        group_name = import_result.get("group") or HIDDIFY_TEST_GROUP
        self.job.log(
            f"Group {group_name!r}: mapped {len(created)}/{len(users)} users "
            f"(new={import_result.get('created_count', 0)}, "
            f"errors={len(errors)})"
        )
        for err in errors[:20]:
            self.job.log(f"✗ {err.get('username')}: {err.get('error')}")

        self.job.set_progress(70, "ساخت mapping ریدایرکت (مثل 3x-ui)...")
        mapping = build_subscription_mapping(
            created,
            proxy_path_client=client_path,
            proxy_path=root_path,
            subscription_domains=sub_domains,
            listen_ports=listen_ports,
        )
        mapping_file = work / "subscription_url_mapping.json"
        mapping_file.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
        # Also keep a copy under PasarGuard data for ops
        try:
            PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mapping_file, PASARGUARD_DATA / "hiddify_subscription_url_mapping.json")
        except Exception as e:
            self.job.log(f"mapping copy note: {e}")

        sample_old = ""
        sample_new = ""
        if created:
            sample = created[0]
            sample_new = sample.get("subscription_url") or ""
            primary = (mapping.get("mappings") or {}).get(sample.get("username") or "") or {}
            sample_old = primary.get("old_subscription_url") or ""

        self.job.set_progress(85, "راه‌اندازی مجدد PasarGuard...")
        await safe_start_pasarguard(self)

        redirect_installed = False
        redirect_error = ""
        https_ports = list(listen_ports.get("https") or [443])
        redirect_port = int(params.get("redirect_port") or https_ports[0] or 443)
        extra_https = [p for p in https_ports if p != redirect_port]
        env_text = (
            PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
            if PASARGUARD_ENV.exists()
            else ""
        )
        redirect_domain = (
            (params.get("redirect_domain") or "").strip()
            or resolve_pasarguard_public_base(env_text)
        )

        if install_redirect and mapping_file.exists():
            self.job.set_progress(92, "نصب pg-redirect: لینک قدیمی هیدیفای → /sub جدید...")
            redirect_installed, redirect_error = await self._install_redirect(
                mapping_file,
                listen_port=redirect_port,
                extra_ports=extra_https,
                redirect_domain=redirect_domain,
                work_dir=work,
                subscription_domains=sub_domains,
            )

        # By design (not a failure): user asked to skip inbounds/proxies.
        warn_en: list[str] = [
            "By design (not an error): only users + subscription redirect. Inbounds/proxies are skipped — configure them in PasarGuard.",
            "Hiddify admin accounts are not migrated — create a PasarGuard owner with the command below.",
        ]
        warn_fa: list[str] = [
            "عمدی است (خطا نیست): فقط کاربران + ریدایرکت لینک اشتراک. اینباند/پروکسی عمداً منتقل نشده — در پاسارگارد تنظیم کنید.",
            "ادمین هیدیفای منتقل نمی‌شود — Owner پاسارگارد را با دستور زیر بسازید.",
        ]
        warn_ru: list[str] = [
            "Намеренно (не ошибка): только пользователи + redirect. Inbound/proxy не переносятся — настройте в PasarGuard.",
            "Админы Hiddify не переносятся — создайте owner PasarGuard командой ниже.",
        ]
        if errors:
            warn_en.append(f"{len(errors)} users failed to import — see logs.")
            warn_fa.append(f"{len(errors)} کاربر وارد نشد — لاگ را ببینید.")
            warn_ru.append(f"{len(errors)} пользователей не импортированы.")
        if not redirect_installed and install_redirect:
            detail = (redirect_error or "").strip()
            if len(detail) > 280:
                detail = "…" + detail[-280:]
            warn_en.append(
                "pg-redirect did NOT install — old Hiddify links will not work until fixed. "
                "Needs free :443 and a service that can bind privileged ports (runs as root). "
                + (f"Cause: {detail}" if detail else "")
            )
            warn_fa.append(
                "pg-redirect نصب نشد — لینک‌های قدیمی کار نمی‌کنند. "
                "پورت ۴۴۳ باید آزاد باشد و سرویس باید بتواند پورت privileged را bind کند (به‌صورت root). "
                + (f"علت: {detail}" if detail else "")
            )
            warn_ru.append(
                "pg-redirect не установился — старые ссылки не работают. "
                "Нужен свободный :443 и сервис с правом bind privileged ports (root). "
                + (f"Причина: {detail}" if detail else "")
            )
        elif redirect_installed:
            warn_en.append(
                "Hiddify web on subscription HTTPS ports was stopped so pg-redirect can serve old paths. "
                "Do not start Hiddify nginx/haproxy on those ports again."
            )
            warn_fa.append(
                "وب هیدیفای روی پورت‌های HTTPS اشتراک متوقف شد تا pg-redirect لینک‌های قدیمی را سرو کند. "
                "دوباره nginx/haproxy هیدیفای را روی آن پورت‌ها روشن نکنید."
            )
            warn_ru.append(
                "Веб Hiddify на HTTPS-портах подписки остановлен, чтобы pg-redirect обслуживал старые ссылки. "
                "Не поднимайте nginx/haproxy Hiddify снова на этих портах."
            )
            if sub_domains:
                warn_en.append(
                    f"Old client hosts should keep pointing at this server: {', '.join(sub_domains[:5])}."
                )
                warn_fa.append(
                    f"دامنه‌های قدیمی اشتراک باید به همین سرور اشاره کنند: {', '.join(sub_domains[:5])}."
                )

        self.job.set_progress(100, f"مهاجرت Hiddify انجام شد — {len(created)} کاربر")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "redirect",
            "subscription_preserved": True,
            "redirect_installed": redirect_installed,
            "redirect_port": redirect_port,
            "redirect_extra_ports": extra_https,
            "redirect_path": client_path or "uuid",
            "redirect_domain": redirect_domain,
            "redirect_scheme": "https",
            "redirect_sample_old": sample_old,
            "redirect_sample_new": sample_new,
            "subscription_domains": sub_domains,
            "mapping_file": str(mapping_file),
            "users_migrated": len(created),
            "users_total": len(users),
            "users_failed": len(errors),
            "group": group_name,
            "proxy_path_client": client_path,
            "summary": summary,
            "show_owner_guide": True,
            "owner_cmd": "pasarguard cli generate-temp-key",
            "incomplete": [
                {
                    "name": {"en": "Inbounds / proxies", "fa": "اینباند / پروکسی", "ru": "Inbound / proxy"},
                    "copied": 0,
                    "source": summary.get("proxies") or 0,
                    "missing": summary.get("proxies") or 0,
                },
                {
                    "name": {"en": "Domains / CDN / Reality", "fa": "دامنه‌ها / CDN / Reality", "ru": "Домены / CDN / Reality"},
                    "copied": 0,
                    "source": summary.get("domains") or 0,
                    "missing": summary.get("domains") or 0,
                },
            ],
            "warnings": {"en": warn_en, "fa": warn_fa, "ru": warn_ru},
        }

    async def _load_backup(
        self,
        upload_path: str | None,
        upload_work_dir: str | None,
        params: dict,
    ) -> dict:
        candidates: list[Path] = []
        for raw in (upload_work_dir, upload_path):
            if not raw:
                continue
            p = Path(raw)
            if p.exists():
                candidates.append(p)

        for src in candidates:
            if src.is_file() and src.suffix.lower() == ".json":
                return load_hiddify_json_file(src)
            if src.is_file() and src.suffix.lower() == ".zip":
                extract_dir = BACKUP_DIR / self.job.job_id / "unzipped"
                extract_dir.mkdir(parents=True, exist_ok=True)
                await self._run_cmd(["unzip", "-o", str(src), "-d", str(extract_dir)])
                found = find_hiddify_json_in_dir(extract_dir)
                if found:
                    return load_hiddify_json_file(found)
            if src.is_dir():
                found = find_hiddify_json_in_dir(src)
                if found:
                    return load_hiddify_json_file(found)
                # SQL dump fallback — extract users via mysql parse is limited;
                # prefer JSON. If only .sql present, try live-style SELECT is N/A.
                sql_files = list(src.rglob("*.sql"))
                if sql_files:
                    raise RuntimeError(
                        "بکاپ SQL هیدیفای پشتیبانی محدود دارد — از بکاپ JSON پنل "
                        "(Export) استفاده کنید."
                    )

        # Live install fallback: current.json or MySQL
        current_json = HIDDIFY_DIR / "current.json"
        if current_json.is_file():
            try:
                return load_hiddify_json_file(current_json)
            except Exception as e:
                self.job.log(f"current.json read failed: {e}")

        password = params.get("source_db_password")
        if not password and HIDDIFY_MYSQL_PASS.exists():
            password = HIDDIFY_MYSQL_PASS.read_text().strip()
        if password:
            live = await self._extract_users_live_as_json(password)
            if live:
                return live

        raise RuntimeError(
            "بکاپ Hiddify یافت نشد — فایل JSON Export را آپلود کنید "
            "(یا current.json / MySQL روی همین سرور)."
        )

    async def _extract_users_live_as_json(self, password: str) -> dict | None:
        """Best-effort live MySQL → pseudo JSON backup (users + proxy paths)."""
        ok, out = await self._run_cmd([
            "mysql", "-u", "hiddifypanel", f"-p{password}",
            "-h", "127.0.0.1", "hiddifypanel", "-N", "-e",
            "SELECT name, uuid, usage_limit_GB, package_days, enable, "
            "current_usage_GB, IFNULL(start_date,''), IFNULL(comment,''), mode "
            "FROM user;",
        ])
        if not ok:
            ok, out = await self._run_cmd([
                "mysql", "-u", "root", f"-p{password}",
                "-h", "127.0.0.1", "hiddifypanel", "-N", "-e",
                "SELECT name, uuid, usage_limit_GB, package_days, enable, "
                "current_usage_GB, IFNULL(start_date,''), IFNULL(comment,''), mode "
                "FROM user;",
            ])
        if not ok:
            self.job.log(f"MySQL Hiddify failed: {out[:200]}")
            return None

        users = []
        for line in (out or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            users.append({
                "name": parts[0],
                "uuid": parts[1],
                "usage_limit_GB": float(parts[2] or 0),
                "package_days": int(float(parts[3] or 0)),
                "enable": str(parts[4]) in ("1", "true", "True"),
                "current_usage_GB": float(parts[5] or 0) if len(parts) > 5 else 0,
                "start_date": parts[6] or None if len(parts) > 6 else None,
                "comment": parts[7] if len(parts) > 7 else "",
                "mode": parts[8] if len(parts) > 8 else "no_reset",
            })

        hconfigs = []
        ok2, out2 = await self._run_cmd([
            "mysql", "-u", "hiddifypanel", f"-p{password}",
            "-h", "127.0.0.1", "hiddifypanel", "-N", "-e",
            "SELECT `key`, value FROM hconfig "
            "WHERE `key` IN ("
            "'proxy_path_client','proxy_path','proxy_path_admin',"
            "'tls_ports','http_ports'"
            ");",
        ])
        if ok2:
            for line in (out2 or "").splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    hconfigs.append({"key": parts[0], "value": parts[1]})

        # Best-effort domains for TLS SAN / sub_link_only detection
        domains = []
        ok3, out3 = await self._run_cmd([
            "mysql", "-u", "hiddifypanel", f"-p{password}",
            "-h", "127.0.0.1", "hiddifypanel", "-N", "-e",
            "SELECT domain, mode, IFNULL(download_domain,''), "
            "IFNULL(sub_link_only,0) FROM domain;",
        ])
        if ok3:
            for line in (out3 or "").splitlines():
                parts = line.split("\t")
                if not parts or not parts[0]:
                    continue
                domains.append({
                    "domain": parts[0],
                    "mode": parts[1] if len(parts) > 1 else "direct",
                    "download_domain": parts[2] if len(parts) > 2 else "",
                    "sub_link_only": str(parts[3] if len(parts) > 3 else "0") in ("1", "true", "True"),
                })

        return {
            "users": users,
            "hconfigs": hconfigs,
            "domains": domains,
            "proxies": [],
            "admin_users": [],
        }

    async def _install_redirect(
        self,
        mapping_file: Path,
        *,
        listen_port: int,
        extra_ports: list[int],
        redirect_domain: str,
        work_dir: Path,
        subscription_domains: list[str],
    ) -> tuple[bool, str]:
        from app.services.redirect_ops import (
            free_listen_port,
            install_pg_redirect,
            needs_privileged_bind,
            pg_redirect_healthz_ok,
            resolve_redirect_tls,
        )

        ports = [int(listen_port), *[int(p) for p in extra_ports]]
        # Hiddify multiplexes panel + client subscription paths on :443 (+ tls_ports).
        self.job.log(
            f"Freeing Hiddify HTTPS ports {ports} for pg-redirect "
            "(old client links hit these ports)…"
        )
        if needs_privileged_bind(ports):
            self.job.log(
                "Hiddify redirect needs privileged bind (:443) — "
                "pg-redirect will run as root (not pgredirect) so listen succeeds"
            )
        # Stop Hiddify web ONCE, then only poke each port (prevents 92% hang).
        await free_listen_port(self, listen_port, panel="hiddify", stop_competing=True)
        for ep in extra_ports:
            await free_listen_port(self, ep, panel="hiddify", stop_competing=False)

        # Prefer real Hiddify certs for old subscription hostnames so TLS stays valid.
        cn = (subscription_domains[0] if subscription_domains else "") or "127.0.0.1"
        cert_pem, key_pem, tls_src = resolve_redirect_tls(
            work_dir=work_dir,
            want_ssl=True,
            common_name=cn,
            san_hosts=subscription_domains,
            prefer_hiddify_ssl=True,
        )
        if tls_src:
            self.job.log(f"pg-redirect TLS: {tls_src}")
        want_ssl = bool(cert_pem and key_pem)

        async def _ok_after(install_ok: bool) -> bool:
            if install_ok:
                return True
            # Never trust systemctl "active" alone — require a live /healthz.
            return await pg_redirect_healthz_ok(
                self, listen_port=listen_port, ssl=want_ssl,
            )

        ok, err = await install_pg_redirect(
            self,
            mapping_file,
            listen_port=listen_port,
            redirect_base=redirect_domain,
            panel="hiddify",
            ssl_cert=cert_pem,
            ssl_key=key_pem,
            extra_ports=extra_ports,
        )
        if await _ok_after(ok):
            return True, ""

        # Retry once after another aggressive free (services may have raced back)
        self.job.log("pg-redirect start failed — freeing ports again and retrying…")
        await free_listen_port(self, listen_port, panel="hiddify", stop_competing=True)
        for ep in extra_ports:
            await free_listen_port(self, ep, panel="hiddify", stop_competing=False)
        if not cert_pem:
            cert_pem, key_pem, tls_src = resolve_redirect_tls(
                work_dir=work_dir,
                want_ssl=True,
                common_name=cn,
                san_hosts=subscription_domains,
                prefer_hiddify_ssl=True,
            )
            if tls_src:
                self.job.log(f"pg-redirect retry TLS: {tls_src}")
            want_ssl = bool(cert_pem and key_pem)
        ok2, err2 = await install_pg_redirect(
            self,
            mapping_file,
            listen_port=listen_port,
            redirect_base=redirect_domain,
            panel="hiddify",
            ssl_cert=cert_pem,
            ssl_key=key_pem,
            extra_ports=extra_ports,
        )
        if await _ok_after(ok2):
            return True, ""
        detail = err2 or err or "pg-redirect install failed"
        low = detail.lower()
        if "address already in use" in low or "errno 98" in low:
            detail += (
                f" — port {listen_port} still busy (Hiddify/nginx/haproxy/docker). "
                f"Stop Hiddify web on that port, then retry."
            )
        if "permission denied" in low or "errno 13" in low or "eacces" in low:
            detail += (
                " — privileged port bind denied; pg-redirect must run as root on :443."
            )
        return False, detail

    def _get_panel_url(self) -> str:
        from app.services.pg_access import get_panel_access_info, build_dashboard_url, resolve_dashboard_path
        from app.config import PASARGUARD_ENV

        access = get_panel_access_info()
        if access.get("login_url"):
            return access["login_url"]
        env_text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore") if PASARGUARD_ENV.exists() else ""
        dash = resolve_dashboard_path(env_text) if env_text else "/dashboard/"
        return build_dashboard_url("127.0.0.1", access.get("port") or "8000", https=True, dashboard_path=dash)
