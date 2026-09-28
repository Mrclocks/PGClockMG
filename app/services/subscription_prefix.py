"""Apply public subscription base (domain/port) for migrate + redirect.

Writes both PasarGuard ``.env`` (pg-redirect reads this live) and the panel
``settings`` row (UI ``url_prefix``). Certbot is optional and always fail-soft.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from app.config import PASARGUARD_DATA, PASARGUARD_ENV
from app.services.env_migration import _set_env_var_simple, read_env_var
from app.services.pg_access import resolve_pasarguard_public_base

_SAFE_HOST = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,}$|^(?:\d{1,3}\.){3}\d{1,3}$"
)


def default_redirect_fields(env_text: str | None = None) -> dict:
    """Defaults for the migrate wizard redirect form."""
    text = env_text
    if text is None and PASARGUARD_ENV.exists():
        try:
            text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    base = resolve_pasarguard_public_base(text or "")
    parsed = urlparse(base)
    host = (parsed.hostname or "").strip()
    port = parsed.port
    if not port:
        raw = (read_env_var(text or "", "UVICORN_PORT") or "8000").strip()
        try:
            port = int(raw)
        except ValueError:
            port = 8000
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        scheme = "https"
    return {
        "base": base,
        "domain": host,
        "port": int(port),
        "scheme": scheme,
    }


def normalize_public_base(
    domain: str | None,
    port: int | str | None = None,
    scheme: str | None = None,
    *,
    fallback: str | None = None,
) -> str:
    """Build ``scheme://host:port`` from wizard fields or fall back."""
    raw = (domain or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        host = (parsed.hostname or "").strip()
        sch = (parsed.scheme or scheme or "https").lower()
        prt = parsed.port
    else:
        host = raw.split("/")[0].split(":")[0].strip().lower().rstrip(".")
        sch = (scheme or "https").lower().strip()
        prt = None
    if not host:
        return (fallback or "").rstrip("/")
    if sch not in ("http", "https"):
        sch = "https"
    if prt is None:
        try:
            prt = int(port) if port not in (None, "") else 8000
        except (TypeError, ValueError):
            prt = 8000
    if prt <= 0 or prt > 65535:
        prt = 8000
    return f"{sch}://{host}:{int(prt)}"


def is_plausible_hostname(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h or len(h) > 253:
        return False
    return bool(_SAFE_HOST.match(h))


def write_subscription_prefix_env(base: str, *, env_path: Path | None = None) -> bool:
    """Set SUBSCRIPTION_URL_PREFIX in PasarGuard .env. Returns True on write."""
    path = env_path or PASARGUARD_ENV
    base = (base or "").strip().rstrip("/")
    if not base:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    except OSError:
        return False
    text = _set_env_var_simple(text, "SUBSCRIPTION_URL_PREFIX", base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


def write_ssl_env_paths(
    cert: Path,
    key: Path,
    *,
    env_path: Path | None = None,
) -> bool:
    path = env_path or PASARGUARD_ENV
    if not cert.is_file() or not key.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    except OSError:
        return False
    text = _set_env_var_simple(text, "UVICORN_SSL_CERTFILE", str(cert))
    text = _set_env_var_simple(text, "UVICORN_SSL_KEYFILE", str(key))
    try:
        path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


def _merge_url_prefix_json(raw: str | None, base: str) -> str:
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
    data["url_prefix"] = base
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def apply_subscription_prefix_sqlite(sqlite_path: str | Path, base: str) -> bool:
    """Upsert settings.subscription.url_prefix on a PasarGuard SQLite file."""
    path = Path(sqlite_path)
    base = (base or "").strip().rstrip("/")
    if not base or not path.is_file():
        return False
    db = sqlite3.connect(str(path))
    try:
        cols = {
            str(r[1]).lower()
            for r in db.execute("PRAGMA table_info(settings)").fetchall()
        }
        if "value" not in cols:
            return False
        key_col = "key" if "key" in cols else ("entity" if "entity" in cols else None)
        if not key_col:
            return False
        row = db.execute(
            f"SELECT value FROM settings WHERE lower({key_col})=lower(?) LIMIT 1",
            ("subscription",),
        ).fetchone()
        new_val = _merge_url_prefix_json(row[0] if row else None, base)
        if row:
            db.execute(
                f"UPDATE settings SET value=? WHERE lower({key_col})=lower(?)",
                (new_val, "subscription"),
            )
        else:
            # Prefer (id, key, value) layout; fall back to (key, value)
            if "id" in cols:
                db.execute(
                    f"INSERT INTO settings (id, {key_col}, value) VALUES ("
                    f"(SELECT COALESCE(MAX(id),0)+1 FROM settings), ?, ?)",
                    ("subscription", new_val),
                )
            else:
                db.execute(
                    f"INSERT INTO settings ({key_col}, value) VALUES (?, ?)",
                    ("subscription", new_val),
                )
        db.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        db.close()


def apply_subscription_prefix_server_db(db_type: str, conn: dict, base: str) -> bool:
    """Upsert subscription url_prefix on live MySQL/PG (fail-soft)."""
    base = (base or "").strip().rstrip("/")
    if not base:
        return False
    db_type = (db_type or "").lower()
    host = conn.get("host") or "127.0.0.1"
    port = int(conn.get("port") or (5432 if db_type in ("postgresql", "timescaledb") else 3306))
    user = conn.get("user") or (
        "postgres" if db_type in ("postgresql", "timescaledb") else "root"
    )
    password = conn.get("password") or ""
    database = conn.get("database") or "pasarguard"
    try:
        if db_type in ("postgresql", "timescaledb"):
            import psycopg2

            with psycopg2.connect(
                host=host, port=port, dbname=database, user=user, password=password,
            ) as pg:
                with pg.cursor() as cur:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='settings'"
                    )
                    cols = {str(r[0]).lower() for r in cur.fetchall()}
                    if "value" not in cols:
                        return False
                    key_col = "key" if "key" in cols else (
                        "entity" if "entity" in cols else None
                    )
                    if not key_col:
                        return False
                    cur.execute(
                        f"SELECT value FROM settings WHERE lower({key_col})=lower(%s) LIMIT 1",
                        ("subscription",),
                    )
                    row = cur.fetchone()
                    new_val = _merge_url_prefix_json(row[0] if row else None, base)
                    if row:
                        cur.execute(
                            f"UPDATE settings SET value=%s WHERE lower({key_col})=lower(%s)",
                            (new_val, "subscription"),
                        )
                    else:
                        cur.execute(
                            f"INSERT INTO settings ({key_col}, value) VALUES (%s, %s)",
                            ("subscription", new_val),
                        )
                pg.commit()
            return True

        if db_type in ("mysql", "mariadb"):
            import pymysql

            with pymysql.connect(
                host=host, port=port, user=user, password=password,
                database=database, charset="utf8mb4", autocommit=True,
            ) as mysql:
                with mysql.cursor() as cur:
                    cur.execute(
                        "SELECT COLUMN_NAME FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name='settings'",
                        (database,),
                    )
                    cols = {str(r[0]).lower() for r in cur.fetchall()}
                    if "value" not in cols:
                        return False
                    key_col = "key" if "key" in cols else (
                        "entity" if "entity" in cols else None
                    )
                    if not key_col:
                        return False
                    cur.execute(
                        f"SELECT value FROM settings WHERE lower({key_col})=lower(%s) LIMIT 1",
                        ("subscription",),
                    )
                    row = cur.fetchone()
                    new_val = _merge_url_prefix_json(row[0] if row else None, base)
                    if row:
                        cur.execute(
                            f"UPDATE settings SET value=%s WHERE lower({key_col})=lower(%s)",
                            (new_val, "subscription"),
                        )
                    else:
                        cur.execute(
                            f"INSERT INTO settings ({key_col}, value) VALUES (%s, %s)",
                            ("subscription", new_val),
                        )
            return True
    except Exception:
        return False
    return False


def _chmod_readable(path: Path) -> None:
    try:
        if path.is_dir():
            os.chmod(path, 0o755)
            for child in path.rglob("*"):
                try:
                    os.chmod(child, 0o755 if child.is_dir() else 0o644)
                except OSError:
                    pass
        elif path.is_file():
            os.chmod(path, 0o644)
    except OSError:
        pass


def install_cert_pair_into_pasarguard(
    cert_src: Path,
    key_src: Path,
    domain: str,
) -> tuple[Path, Path] | None:
    """Copy cert/key into ``/var/lib/pasarguard/certs/<domain>/`` with readable perms."""
    host = (domain or "").strip().lower().rstrip(".")
    if not host or not cert_src.is_file() or not key_src.is_file():
        return None
    dest_dir = PASARGUARD_DATA / "certs" / host
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        cert_dst = dest_dir / "fullchain.pem"
        key_dst = dest_dir / "privkey.pem"
        shutil.copy2(cert_src, cert_dst)
        shutil.copy2(key_src, key_dst)
        _chmod_readable(dest_dir)
        return cert_dst, key_dst
    except OSError:
        return None


def try_certbot_issue(
    domains: list[str],
    *,
    email: str | None = None,
    work_dir: Path | None = None,
) -> dict:
    """Optional Let's Encrypt issue (standalone). Never raises.

    Returns ``{ok, domains, cert, key, error, skipped}``.
    """
    hosts: list[str] = []
    for d in domains or []:
        h = (d or "").strip().lower().rstrip(".")
        if h.startswith("http://") or h.startswith("https://"):
            h = urlparse(h).hostname or ""
        h = (h or "").split(":")[0].strip().lower().rstrip(".")
        if h and is_plausible_hostname(h) and not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", h):
            if h not in hosts:
                hosts.append(h)
    if not hosts:
        return {
            "ok": False,
            "skipped": True,
            "domains": [],
            "cert": "",
            "key": "",
            "error": "no valid domains for certbot",
        }
    certbot = shutil.which("certbot")
    if not certbot:
        return {
            "ok": False,
            "skipped": True,
            "domains": hosts,
            "cert": "",
            "key": "",
            "error": "certbot not installed",
        }

    wd = Path(work_dir) if work_dir else PASARGUARD_DATA / "certbot-work"
    try:
        wd.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {
            "ok": False,
            "skipped": False,
            "domains": hosts,
            "cert": "",
            "key": "",
            "error": f"work dir: {e}",
        }

    primary = hosts[0]
    cmd = [
        certbot, "certonly", "--standalone", "--non-interactive", "--agree-tos",
        "--preferred-challenges", "http",
        "--keep-until-expiring",
        "-d", primary,
    ]
    for extra in hosts[1:]:
        cmd.extend(["-d", extra])
    if email:
        cmd.extend(["--email", email])
    else:
        cmd.append("--register-unsafely-without-email")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except Exception as e:
        return {
            "ok": False,
            "skipped": False,
            "domains": hosts,
            "cert": "",
            "key": "",
            "error": str(e),
        }

    live = Path("/etc/letsencrypt/live") / primary
    cert = live / "fullchain.pem"
    key = live / "privkey.pem"
    if proc.returncode == 0 and cert.is_file() and key.is_file():
        installed = install_cert_pair_into_pasarguard(cert, key, primary)
        if installed:
            return {
                "ok": True,
                "skipped": False,
                "domains": hosts,
                "cert": str(installed[0]),
                "key": str(installed[1]),
                "error": "",
            }
        return {
            "ok": False,
            "skipped": False,
            "domains": hosts,
            "cert": str(cert),
            "key": str(key),
            "error": "issued but failed to copy into pasarguard certs/",
        }

    err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    if len(err) > 800:
        err = "…" + err[-800:]
    return {
        "ok": False,
        "skipped": False,
        "domains": hosts,
        "cert": "",
        "key": "",
        "error": err or f"certbot exit {proc.returncode}",
    }


def manual_cert_guide(domains: list[str], *, lang: str = "en") -> list[str]:
    """Short manual steps when certbot was skipped/failed."""
    joined = ", ".join(domains) if domains else "your-domain.com"
    if lang == "fa":
        return [
            f"برای دامنه‌(ها)ی {joined} یک سرت multi-domain (SAN) بگیرید یا آپلود کنید.",
            "فایل‌ها را در /var/lib/pasarguard/certs/<دامنه>/ بگذارید "
            "(fullchain.pem و privkey.pem) و دسترسی خواندن بدهید.",
            "در .env مقدارهای UVICORN_SSL_CERTFILE و UVICORN_SSL_KEYFILE را به همان مسیرها "
            "تنظیم و پنل + pg-redirect را ری‌استارت کنید.",
            "قبل از صدور: DNS هر دامنه به این سرور و پورت 80 آزاد باشد.",
        ]
    if lang == "ru":
        return [
            f"Получите или загрузите multi-domain (SAN) сертификат для: {joined}.",
            "Положите fullchain.pem и privkey.pem в "
            "/var/lib/pasarguard/certs/<domain>/ с правами чтения.",
            "Пропишите UVICORN_SSL_CERTFILE / UVICORN_SSL_KEYFILE в .env и "
            "перезапустите панель + pg-redirect.",
            "Перед выпуском: DNS на этот сервер и свободный порт 80.",
        ]
    return [
        f"Obtain or upload a multi-domain (SAN) certificate for: {joined}.",
        "Place fullchain.pem and privkey.pem under "
        "/var/lib/pasarguard/certs/<domain>/ with read permissions.",
        "Set UVICORN_SSL_CERTFILE and UVICORN_SSL_KEYFILE in .env, then restart "
        "the panel and pg-redirect.",
        "Before issuing: point DNS to this server and free port 80.",
    ]
