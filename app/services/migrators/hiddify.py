"""Hiddify Manager → PasarGuard migration (users + subscription redirect).

Priority: keep old Hiddify client links working via pg-redirect.
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
    find_hiddify_json_in_dir,
    load_hiddify_json_file,
    parse_users_from_backup,
    summarize_backup,
)
from app.services.pasarguard_ops import safe_start_pasarguard
from app.services.pg_access import resolve_pasarguard_public_base


# Python script executed inside the PasarGuard container (any target DB).
_IMPORT_SCRIPT = r'''
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

payload_path = Path(sys.argv[1])
result_path = Path(sys.argv[2])
payload = json.loads(payload_path.read_text(encoding="utf-8"))
users = payload.get("users") or []

async def main():
    from app.db import GetDB
    from app.db.crud.admin import get_owner
    from app.db.crud.group import get_group
    from app.db.crud.user import create_user, get_user
    from app.models.group import GroupListQuery
    from app.models.proxy import ProxyTable, VlessSettings, VMessSettings, TrojanSettings
    from app.models.user import UserCreate
    from app.utils.jwt import create_subscription_token

    created = []
    errors = []
    skipped = []

    async with GetDB() as db:
        owner = await get_owner(db)
        if owner is None:
            result_path.write_text(json.dumps({
                "ok": False,
                "error": "No PasarGuard owner found — create owner first (pasarguard cli generate-temp-key)",
                "created": [],
                "errors": [],
                "skipped": [],
            }, ensure_ascii=False), encoding="utf-8")
            return

        try:
            groups, _total = await get_group(db, GroupListQuery(limit=100))
            groups = list(groups or [])
        except Exception:
            groups = []

        group_ids = [int(g.id) for g in groups if getattr(g, "id", None) is not None]

        for row in users:
            username = (row.get("username") or "").strip()
            uuid_s = (row.get("uuid") or "").strip()
            if not username or not uuid_s:
                skipped.append({"username": username, "reason": "missing username/uuid"})
                continue
            try:
                uid = UUID(uuid_s)
            except Exception as e:
                errors.append({"username": username, "error": f"bad uuid: {e}"})
                continue

            existing = await get_user(
                db, username,
                load_admin=False, load_next_plan=False,
                load_usage_logs=False, load_groups=False,
            )
            if existing is not None:
                skipped.append({"username": username, "reason": "already_exists", "user_id": int(existing.id)})
                try:
                    token = await create_subscription_token(int(existing.id))
                    created.append({
                        "username": username,
                        "uuid": uuid_s,
                        "user_id": int(existing.id),
                        "subscription_url": f"/sub/{token}",
                        "reused": True,
                    })
                except Exception as e:
                    errors.append({"username": username, "error": f"exists but token failed: {e}"})
                continue

            proxy = ProxyTable(
                vless=VlessSettings(id=uid),
                vmess=VMessSettings(id=uid),
                trojan=TrojanSettings(password=(uuid_s.replace("-", "")[:22] or "hiddify-migrate-pass00")),
            )
            status = (row.get("status") or "active")
            body = {
                "username": username,
                "status": status,
                "data_limit": int(row.get("data_limit") or 0) or None,
                "data_limit_reset_strategy": row.get("data_limit_reset_strategy") or "no_reset",
                "note": (row.get("note") or "")[:500] or None,
                "proxy_settings": proxy,
                "group_ids": group_ids,
            }
            if row.get("expire"):
                body["expire"] = int(row["expire"])
            if row.get("on_hold_expire_duration") and status == "on_hold":
                body["on_hold_expire_duration"] = int(row["on_hold_expire_duration"])

            try:
                new_user = UserCreate(**body)
            except Exception:
                body["status"] = "active" if status != "disabled" else "disabled"
                body.pop("on_hold_expire_duration", None)
                body.pop("on_hold_timeout", None)
                try:
                    new_user = UserCreate(**body)
                except Exception as e2:
                    errors.append({"username": username, "error": f"validate: {e2}"})
                    continue

            try:
                user = await create_user(db, new_user, groups=groups, admin=owner)
                token = await create_subscription_token(int(user.id))
                used = int(row.get("used_traffic") or 0)
                if used > 0:
                    try:
                        user.used_traffic = used
                        await db.commit()
                    except Exception:
                        pass
                created.append({
                    "username": username,
                    "uuid": uuid_s,
                    "user_id": int(user.id),
                    "subscription_url": f"/sub/{token}",
                    "reused": False,
                })
            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
                errors.append({"username": username, "error": str(e)[:300]})

    result_path.write_text(json.dumps({
        "ok": True,
        "created": created,
        "errors": errors,
        "skipped": skipped,
        "created_count": len([c for c in created if not c.get("reused")]),
        "mapped_count": len(created),
    }, ensure_ascii=False), encoding="utf-8")

asyncio.run(main())
'''


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

        self.job.set_progress(15, "خواندن بکاپ Hiddify...")
        data = await self._load_backup(upload_path, upload_work_dir, params)
        summary = summarize_backup(data)
        users, paths = parse_users_from_backup(data)
        if not users:
            raise RuntimeError("هیچ کاربر معتبری در بکاپ Hiddify یافت نشد")

        client_path = paths.get("proxy_path_client") or ""
        root_path = paths.get("proxy_path") or ""
        if not client_path:
            self.job.log("هشدار: proxy_path_client در بکاپ خالی است — ریدایرکت ممکن است ناقص باشد")

        self.job.log(
            f"Hiddify backup: {summary['users_total']} users "
            f"(enabled={summary['users_enabled']}), "
            f"proxy_path_client={client_path!r}"
        )

        self.job.set_progress(35, "ایجاد کاربران در PasarGuard (با همان UUID)...")
        import_result = await self._import_users_in_panel(users, work)
        created = import_result.get("created") or []
        errors = import_result.get("errors") or []
        if not created:
            detail = errors[0].get("error") if errors else import_result.get("error") or "unknown"
            raise RuntimeError(f"ایجاد کاربران در PasarGuard ناموفق بود: {detail}")

        self.job.log(
            f"Mapped {len(created)}/{len(users)} users "
            f"(new={import_result.get('created_count', 0)}, "
            f"errors={len(errors)})"
        )
        for err in errors[:20]:
            self.job.log(f"✗ {err.get('username')}: {err.get('error')}")

        self.job.set_progress(70, "ساخت mapping ریدایرکت لینک‌های قدیمی...")
        mapping = build_subscription_mapping(
            created,
            proxy_path_client=client_path,
            proxy_path=root_path,
        )
        mapping_file = work / "subscription_url_mapping.json"
        mapping_file.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
        # Also keep a copy under PasarGuard data for ops
        try:
            PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mapping_file, PASARGUARD_DATA / "hiddify_subscription_url_mapping.json")
        except Exception as e:
            self.job.log(f"mapping copy note: {e}")

        self.job.set_progress(85, "راه‌اندازی مجدد PasarGuard...")
        await safe_start_pasarguard(self)

        redirect_installed = False
        redirect_error = ""
        redirect_port = int(params.get("redirect_port") or 443)
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
            self.job.set_progress(92, "نصب pg-redirect برای لینک‌های قدیمی Hiddify...")
            redirect_installed, redirect_error = await self._install_redirect(
                mapping_file,
                listen_port=redirect_port,
                redirect_domain=redirect_domain,
                work_dir=work,
            )

        warn_en: list[str] = [
            "Partial migration: users + subscription redirects only. Inbounds/proxies are NOT migrated — configure them in PasarGuard.",
        ]
        warn_fa: list[str] = [
            "مهاجرت ناقص: فقط کاربران و ریدایرکت لینک اشتراک. اینباند/پروکسی منتقل نشده — در پاسارگارد تنظیم کنید.",
        ]
        warn_ru: list[str] = [
            "Частичная миграция: только пользователи и redirect подписки. Inbound/proxy не переносятся.",
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
                + (f"Cause: {detail}" if detail else "")
            )
            warn_fa.append(
                "pg-redirect نصب نشد — لینک‌های قدیمی هیدیفای کار نمی‌کنند. "
                + (f"علت: {detail}" if detail else "")
            )
            warn_ru.append(
                "pg-redirect не установился — старые ссылки Hiddify не работают. "
                + (f"Причина: {detail}" if detail else "")
            )

        self.job.set_progress(100, f"مهاجرت Hiddify انجام شد — {len(created)} کاربر")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "redirect",
            "subscription_preserved": True,
            "redirect_installed": redirect_installed,
            "redirect_port": redirect_port,
            "redirect_path": client_path or "uuid",
            "redirect_domain": redirect_domain,
            "redirect_scheme": "https",
            "mapping_file": str(mapping_file),
            "users_migrated": len(created),
            "users_total": len(users),
            "users_failed": len(errors),
            "proxy_path_client": client_path,
            "summary": summary,
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
            "WHERE `key` IN ('proxy_path_client','proxy_path','proxy_path_admin');",
        ])
        if ok2:
            for line in (out2 or "").splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    hconfigs.append({"key": parts[0], "value": parts[1]})

        return {"users": users, "hconfigs": hconfigs, "domains": [], "proxies": [], "admin_users": []}

    async def _import_users_in_panel(self, users: list[dict], work: Path) -> dict:
        """Import via in-container Python (works for any PasarGuard DB engine)."""
        PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
        stamp = self.job.job_id
        payload_host = PASARGUARD_DATA / f"hiddify_import_{stamp}.json"
        result_host = PASARGUARD_DATA / f"hiddify_import_{stamp}_result.json"
        script_host = PASARGUARD_DATA / f"hiddify_import_{stamp}.py"

        payload_host.write_text(
            json.dumps({"users": users}, ensure_ascii=False),
            encoding="utf-8",
        )
        script_host.write_text(_IMPORT_SCRIPT, encoding="utf-8")
        if result_host.exists():
            result_host.unlink()

        payload_c = f"/var/lib/pasarguard/{payload_host.name}"
        result_c = f"/var/lib/pasarguard/{result_host.name}"
        script_c = f"/var/lib/pasarguard/{script_host.name}"

        # Ensure panel is up enough to exec
        await self._run_cmd(
            ["docker", "compose", "up", "-d", "pasarguard"],
            cwd=str(PASARGUARD_DIR),
            timeout=120,
        )

        ok, out = await self._run_cmd(
            [
                "docker", "compose", "exec", "-T", "pasarguard",
                "python", script_c, payload_c, result_c,
            ],
            cwd=str(PASARGUARD_DIR),
            timeout=max(180, min(1800, 20 + len(users) * 2)),
        )
        if not result_host.is_file():
            # Fallback service name
            for svc in ("panel", "app", "pg"):
                ok2, out2 = await self._run_cmd(
                    [
                        "docker", "compose", "exec", "-T", svc,
                        "python", script_c, payload_c, result_c,
                    ],
                    cwd=str(PASARGUARD_DIR),
                    timeout=max(180, min(1800, 20 + len(users) * 2)),
                )
                if result_host.is_file():
                    ok, out = ok2, out2
                    break

        if not result_host.is_file():
            return {
                "ok": False,
                "error": (out or "import script produced no result")[:500],
                "created": [],
                "errors": [{"username": "*", "error": (out or "no result")[:300]}],
                "skipped": [],
            }

        try:
            result = json.loads(result_host.read_text(encoding="utf-8"))
        except Exception as e:
            return {
                "ok": False,
                "error": f"bad result json: {e}",
                "created": [],
                "errors": [{"username": "*", "error": str(e)}],
                "skipped": [],
            }

        # Cleanup temp scripts (keep result for debug)
        for p in (script_host, payload_host):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        return result

    async def _install_redirect(
        self,
        mapping_file: Path,
        *,
        listen_port: int,
        redirect_domain: str,
        work_dir: Path,
    ) -> tuple[bool, str]:
        from app.services.redirect_ops import (
            free_listen_port,
            install_pg_redirect,
            pg_redirect_is_active,
            resolve_redirect_tls,
        )

        # Stop Hiddify web stack so 443 is free for redirect
        await self._run_cmd(
            ["bash", "-c", "systemctl stop hiddify-panel 2>/dev/null || true"],
            timeout=60,
        )
        await self._run_cmd(
            ["bash", "-c", "systemctl stop hiddify-nginx 2>/dev/null || true"],
            timeout=60,
        )
        await self._run_cmd(
            ["bash", "-c", "systemctl stop nginx 2>/dev/null || true"],
            timeout=30,
        )
        await free_listen_port(self, listen_port)

        cert_pem, key_pem, tls_src = resolve_redirect_tls(
            work_dir=work_dir,
            want_ssl=True,
            common_name="127.0.0.1",
        )
        if tls_src:
            self.job.log(f"pg-redirect TLS: {tls_src}")

        ok, err = await install_pg_redirect(
            self,
            mapping_file,
            listen_port=listen_port,
            redirect_base=redirect_domain,
            panel="hiddify",
            ssl_cert=cert_pem,
            ssl_key=key_pem,
        )
        if ok or await pg_redirect_is_active(self):
            return True, ""

        # Retry with forced self-signed if first attempt lacked certs
        if not cert_pem:
            cert_pem, key_pem, tls_src = resolve_redirect_tls(
                work_dir=work_dir,
                want_ssl=True,
                common_name="127.0.0.1",
            )
            if cert_pem:
                self.job.log(f"pg-redirect retry TLS: {tls_src}")
                ok2, err2 = await install_pg_redirect(
                    self,
                    mapping_file,
                    listen_port=listen_port,
                    redirect_base=redirect_domain,
                    panel="hiddify",
                    ssl_cert=cert_pem,
                    ssl_key=key_pem,
                )
                if ok2 or await pg_redirect_is_active(self):
                    return True, ""
                return False, err2 or err or "pg-redirect install failed"
        return False, err or "pg-redirect install failed"

    def _get_panel_url(self) -> str:
        from app.services.pg_access import get_panel_access_info

        return get_panel_access_info().get("login_url") or "https://127.0.0.1:8000/dashboard/"
