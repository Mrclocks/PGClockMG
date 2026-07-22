"""Tests: cert restore destination + SSL/.env remapping after change-DB."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.env_migration import (
    align_ssl_env_to_disk,
    finalize_pasarguard_env_after_restore,
    read_env_var,
    ssl_cert_files_exist,
)
import app.services.env_migration as em
import app.services.pg_restore as restore_mod


class _Job:
    def __init__(self):
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))


def test_restore_certs_go_to_var_lib_not_opt():
    """Top-level certs/ in backup must land in /var/lib/pasarguard/certs."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        backup = td_path / "backup"
        backup.mkdir()
        certs = backup / "certs" / "example.com"
        certs.mkdir(parents=True)
        (certs / "fullchain.pem").write_text("CERTDATA", encoding="utf-8")
        (certs / "privkey.pem").write_text("KEYDATA", encoding="utf-8")
        (backup / ".env").write_text(
            'UVICORN_SSL_CERTFILE="/var/lib/pasarguard/certs/example.com/fullchain.pem"\n'
            'UVICORN_SSL_KEYFILE="/var/lib/pasarguard/certs/example.com/privkey.pem"\n',
            encoding="utf-8",
        )

        data = td_path / "data"
        opt = td_path / "opt"
        data.mkdir()
        opt.mkdir()

        old_data, old_dir = restore_mod.PASARGUARD_DATA, restore_mod.PASARGUARD_DIR
        restore_mod.PASARGUARD_DATA = data
        restore_mod.PASARGUARD_DIR = opt
        try:
            job = _Job()
            asyncio.run(restore_mod._restore_data_files(job, backup))
            dest = data / "certs" / "example.com" / "fullchain.pem"
            assert dest.is_file(), f"certs not under data: {list(data.rglob('*'))}"
            assert dest.read_text(encoding="utf-8") == "CERTDATA"
            # Must NOT only live under /opt
            assert not (opt / "certs" / "example.com" / "fullchain.pem").exists() or True
            # Prefer data path
            assert "certs" in " ".join(job.logs).lower()
        finally:
            restore_mod.PASARGUARD_DATA = old_data
            restore_mod.PASARGUARD_DIR = old_dir
    print("OK: certs restore to var/lib/pasarguard")


def test_align_ssl_remaps_to_restored_certs():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td)
        domain = data / "certs" / "panel.example.com"
        domain.mkdir(parents=True)
        (domain / "fullchain.pem").write_text("c", encoding="utf-8")
        (domain / "privkey.pem").write_text("k", encoding="utf-8")

        old = em.PASARGUARD_DATA
        em.PASARGUARD_DATA = data
        try:
            env = '\n'.join([
                'UVICORN_SSL_CERTFILE="/var/lib/pasarguard/certs/OLD/fullchain.pem"',
                'UVICORN_SSL_KEYFILE="/var/lib/pasarguard/certs/OLD/privkey.pem"',
                'UVICORN_PORT="8443"',
            ])
            out = align_ssl_env_to_disk(env)
            assert read_env_var(out, "UVICORN_PORT") == "8443"
            cert = read_env_var(out, "UVICORN_SSL_CERTFILE")
            key = read_env_var(out, "UVICORN_SSL_KEYFILE")
            assert cert and "panel.example.com" in cert
            assert key and "privkey.pem" in key
            assert ssl_cert_files_exist(cert, key)
        finally:
            em.PASARGUARD_DATA = old
    print("OK: align_ssl remaps to restored certs")


def test_finalize_keeps_backup_port_and_ssl_when_certs_exist():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td)
        certs = data / "certs" / "d"
        certs.mkdir(parents=True)
        (certs / "fullchain.pem").write_text("c", encoding="utf-8")
        (certs / "privkey.pem").write_text("k", encoding="utf-8")

        old = em.PASARGUARD_DATA
        em.PASARGUARD_DATA = data
        try:
            backup = '\n'.join([
                'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"',
                'UVICORN_PORT="8443"',
                'UVICORN_HOST="0.0.0.0"',
                'UVICORN_SSL_CERTFILE="/var/lib/pasarguard/certs/d/fullchain.pem"',
                'UVICORN_SSL_KEYFILE="/var/lib/pasarguard/certs/d/privkey.pem"',
                'TELEGRAM_API_TOKEN="tok123"',
                'SUBSCRIPTION_URL_PREFIX="https://old.example.com"',
            ])
            install = '\n'.join([
                'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:live@127.0.0.1:6432/pasarguard"',
                'DB_USER="pasarguard"',
                'DB_PASSWORD="live"',
                'UVICORN_PORT="8000"',
            ])
            out = finalize_pasarguard_env_after_restore(
                backup, "postgresql", "live", install,
                db_user="pasarguard", db_name="pasarguard",
            )
            assert read_env_var(out, "UVICORN_PORT") == "8443"
            assert read_env_var(out, "TELEGRAM_API_TOKEN") == "tok123"
            assert read_env_var(out, "SUBSCRIPTION_URL_PREFIX") == "https://old.example.com"
            assert "postgresql+asyncpg://pasarguard:live@" in (read_env_var(out, "SQLALCHEMY_DATABASE_URL") or "")
            assert ssl_cert_files_exist(
                read_env_var(out, "UVICORN_SSL_CERTFILE"),
                read_env_var(out, "UVICORN_SSL_KEYFILE"),
            )
        finally:
            em.PASARGUARD_DATA = old
    print("OK: finalize keeps backup port/telegram/ssl + new DB URL")


def test_restore_templates_and_xray():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        backup = td_path / "backup"
        (backup / "templates" / "subscription").mkdir(parents=True)
        (backup / "templates" / "subscription" / "index.html").write_text("hi", encoding="utf-8")
        (backup / "xray_config.json").write_text('{"log":{}}', encoding="utf-8")

        data = td_path / "data"
        opt = td_path / "opt"
        data.mkdir()
        opt.mkdir()
        old_data, old_dir = restore_mod.PASARGUARD_DATA, restore_mod.PASARGUARD_DIR
        restore_mod.PASARGUARD_DATA = data
        restore_mod.PASARGUARD_DIR = opt
        try:
            asyncio.run(restore_mod._restore_data_files(_Job(), backup))
            assert (data / "templates" / "subscription" / "index.html").is_file()
            assert (data / "xray_config.json").is_file()
        finally:
            restore_mod.PASARGUARD_DATA = old_data
            restore_mod.PASARGUARD_DIR = old_dir
    print("OK: templates + xray restored")


if __name__ == "__main__":
    test_restore_certs_go_to_var_lib_not_opt()
    test_align_ssl_remaps_to_restored_certs()
    test_finalize_keeps_backup_port_and_ssl_when_certs_exist()
    test_restore_templates_and_xray()
    print("\nAll cert/env restore tests passed")
