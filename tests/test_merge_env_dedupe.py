"""Regression: merge/finalize must not leave duplicate SQLALCHEMY URLs."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

from app.services.env_migration import _sqlalchemy_url_line_pattern, env_points_to_db
from app.services.pg_restore import _merge_env_after_restore


def test_merge_strips_duplicate_sqlite_urls_on_hard_convert(tmp_path, monkeypatch):
    import app.services.pg_restore as pr

    env_file = tmp_path / ".env"
    monkeypatch.setattr(pr, "PASARGUARD_ENV", env_file)

    backup = "\n".join([
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'UVICORN_PORT = 8880',
        'TELEGRAM_API_TOKEN = "tok"',
    ])
    install = "\n".join([
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:x@127.0.0.1:6432/pasarguard"',
        'DB_PASSWORD="x"',
        'POSTGRES_PASSWORD="x"',
    ])
    job = MagicMock()
    # Hard convert: do NOT preserve SQLALCHEMY (finalize writes it later)
    preserve = {"DB_PASSWORD": "x", "POSTGRES_PASSWORD": "x"}
    asyncio.run(
        _merge_env_after_restore(job, backup, install, preserve, target_db="timescaledb")
    )
    text = env_file.read_text(encoding="utf-8")
    n = len(re.findall(_sqlalchemy_url_line_pattern(), text))
    assert n == 0, f"hard-convert merge must strip backup URLs, got {n}: {text}"
    assert "TELEGRAM_API_TOKEN" in text
    assert "8880" in text
    from app.services.env_migration import read_env_var
    assert read_env_var(text, "PASARGUARD_DB_ENGINE") == "timescaledb"


def test_merge_keeps_install_url_and_stamp_on_hard_convert(tmp_path, monkeypatch):
    """sqlite→server convert must not leave backup sqlite URL/stamp in live .env."""
    import app.services.pg_restore as pr
    from app.services.env_migration import read_env_var

    env_file = tmp_path / ".env"
    monkeypatch.setattr(pr, "PASARGUARD_ENV", env_file)

    install_url = "postgresql+asyncpg://pasarguard:live@127.0.0.1:6432/pasarguard"
    backup = "\n".join([
        'PASARGUARD_DB_ENGINE="sqlite"',
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'UVICORN_PORT = 8880',
        'TELEGRAM_API_TOKEN = "tok"',
    ])
    install = "\n".join([
        f'SQLALCHEMY_DATABASE_URL="{install_url}"',
        'PASARGUARD_DB_ENGINE="timescaledb"',
        'DB_PASSWORD="live"',
        'POSTGRES_PASSWORD="live"',
    ])
    job = MagicMock()
    preserve = {
        "DB_PASSWORD": "live",
        "POSTGRES_PASSWORD": "live",
        "PASARGUARD_DB_ENGINE": "timescaledb",
        "SQLALCHEMY_DATABASE_URL": install_url,
    }
    asyncio.run(
        _merge_env_after_restore(job, backup, install, preserve, target_db="timescaledb")
    )
    text = env_file.read_text(encoding="utf-8")
    lines = re.findall(_sqlalchemy_url_line_pattern(), text)
    assert len(lines) == 1
    assert "sqlite" not in lines[0].lower()
    assert env_points_to_db(text, "timescaledb")
    assert read_env_var(text, "PASARGUARD_DB_ENGINE") == "timescaledb"
    assert "TELEGRAM_API_TOKEN" in text
    assert "8880" in text


def test_rollback_env_after_failed_convert(tmp_path, monkeypatch):
    import app.services.pg_restore as pr
    from app.services.env_migration import read_env_var
    from app.services.pg_restore import _rollback_env_after_failed_convert

    env_file = tmp_path / ".env"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sqlite_path = data_dir / "db.sqlite3"
    sqlite_path.write_text("intermediate", encoding="utf-8")
    monkeypatch.setattr(pr, "PASARGUARD_ENV", env_file)
    monkeypatch.setattr(pr, "PASARGUARD_DATA", data_dir)

    # Simulate mid-convert .env stuck on sqlite from backup merge
    env_file.write_text(
        'PASARGUARD_DB_ENGINE="sqlite"\n'
        'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\n',
        encoding="utf-8",
    )
    install = "\n".join([
        'PASARGUARD_DB_ENGINE="timescaledb"',
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://pasarguard:x@127.0.0.1:6432/pasarguard"',
        'DB_PASSWORD="x"',
        'POSTGRES_PASSWORD="x"',
    ])
    job = MagicMock()
    job.job_id = "testjob"
    _rollback_env_after_failed_convert(job, install, "timescaledb")
    text = env_file.read_text(encoding="utf-8")
    assert read_env_var(text, "PASARGUARD_DB_ENGINE") == "timescaledb"
    assert "sqlite" not in (read_env_var(text, "SQLALCHEMY_DATABASE_URL") or "").lower()
    assert not sqlite_path.exists()
    assert (data_dir / "db.sqlite3.failed-convert-testjob.bak").exists()
    failed_bak = env_file.with_suffix(".env.bak-failed-convert")
    assert failed_bak.exists()
    import app.services.pg_restore as pr

    env_file = tmp_path / ".env"
    monkeypatch.setattr(pr, "PASARGUARD_ENV", env_file)

    backup = "\n".join([
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://///var/lib/pasarguard/db.sqlite3"',
        'UVICORN_PORT = 8880',
    ])
    install_url = "postgresql+asyncpg://pasarguard:x@127.0.0.1:6432/pasarguard"
    install = f'SQLALCHEMY_DATABASE_URL="{install_url}"\nDB_PASSWORD="x"\n'
    job = MagicMock()
    preserve = {"SQLALCHEMY_DATABASE_URL": install_url, "DB_PASSWORD": "x"}
    asyncio.run(
        _merge_env_after_restore(job, backup, install, preserve, target_db="timescaledb")
    )
    text = env_file.read_text(encoding="utf-8")
    lines = re.findall(_sqlalchemy_url_line_pattern(), text)
    assert len(lines) == 1
    assert "sqlite" not in lines[0].lower()
    assert env_points_to_db(text, "timescaledb")


def test_merge_strips_postgres_keys_when_target_mysql(tmp_path, monkeypatch):
    import app.services.pg_restore as pr

    env_file = tmp_path / ".env"
    monkeypatch.setattr(pr, "PASARGUARD_ENV", env_file)

    backup = "\n".join([
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://u:old@127.0.0.1:6432/pasarguard"',
        'POSTGRES_PASSWORD="old"',
        'DB_PASSWORD="old"',
        'TELEGRAM_API_TOKEN="tok"',
    ])
    install = "\n".join([
        'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://u:live@127.0.0.1:3306/pasarguard"',
        'DB_PASSWORD="live"',
        'MYSQL_ROOT_PASSWORD="live"',
    ])
    job = MagicMock()
    preserve = {"DB_PASSWORD": "live", "MYSQL_ROOT_PASSWORD": "live"}
    asyncio.run(_merge_env_after_restore(job, backup, install, preserve, target_db="mysql"))
    text = env_file.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD" not in text
    assert "MYSQL_ROOT_PASSWORD" in text
    assert "TELEGRAM_API_TOKEN" in text
