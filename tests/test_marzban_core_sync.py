"""Regression: Marzban xray_config.json on disk must replace PasarGuard core/inbounds.

Covers the reported failure mode where hosts migrate, the JSON file is present under
/var/lib/pasarguard/, but the panel still shows the install-default inbound because
core_configs was never overwritten.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _marzban_xray(*, tags: list[str] | None = None) -> str:
    tags = tags or ["VLESS-Reality", "VMess-WS", "Shadowsocks"]
    inbounds = []
    for i, tag in enumerate(tags):
        proto = "vless" if "LESS" in tag.upper() or "VLESS" in tag.upper() else (
            "vmess" if "VMESS" in tag.upper() else "shadowsocks"
        )
        inbounds.append({
            "tag": tag,
            "protocol": proto,
            "port": 10000 + i,
            "settings": {},
            "streamSettings": {"network": "tcp"},
        })
    return json.dumps({"log": {"loglevel": "warning"}, "inbounds": inbounds, "outbounds": [{"protocol": "freedom"}]}, indent=2)


def _default_pg_xray() -> str:
    return json.dumps({
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "Shadowsocks TCP",
            "protocol": "shadowsocks",
            "port": 1080,
            "settings": {},
        }],
        "outbounds": [{"protocol": "freedom"}],
    })


def _sqlite_with_default_core(path: Path, *, users: int = 2, hosts: int = 2) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE hosts (id INTEGER PRIMARY KEY, remark TEXT, inbound_tag TEXT);
        CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, is_disabled INTEGER DEFAULT 0);
        CREATE TABLE core_configs (id INTEGER PRIMARY KEY, name TEXT, config TEXT);
        CREATE TABLE admins (id INTEGER PRIMARY KEY);
        CREATE TABLE nodes (id INTEGER PRIMARY KEY);
        CREATE TABLE groups (id INTEGER PRIMARY KEY);
        """
    )
    for i in range(users):
        conn.execute("INSERT INTO users VALUES (?, ?)", (i + 1, f"u{i}"))
    for i in range(hosts):
        conn.execute(
            "INSERT INTO hosts VALUES (?, ?, ?)",
            (i + 1, f"h{i}", "VLESS-Reality"),
        )
    # Install-default inbound + core (the bug state)
    conn.execute(
        "INSERT INTO inbounds VALUES (1, 'Shadowsocks TCP', 'shadowsocks', 0)"
    )
    conn.execute(
        "INSERT INTO core_configs VALUES (1, 'Default', ?)",
        (_default_pg_xray(),),
    )
    conn.commit()
    conn.close()
    return path


def test_force_env_sqlite_re_pins_xray_json(tmp_path):
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    pg_dir = tmp_path / "opt"
    pg_data = tmp_path / "data"
    pg_dir.mkdir()
    pg_data.mkdir()
    env = pg_dir / ".env"
    env.write_text(
        'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\n'
        'XRAY_JSON="./xray_config.json"\n',
        encoding="utf-8",
    )
    (pg_data / "xray_config.json").write_text(_marzban_xray(), encoding="utf-8")
    bak = tmp_path / "bak"
    bak.mkdir()

    m = MarzbanMigrator(MigrationJob(job_id="pin"), {})
    with (
        patch("app.services.migrators.marzban.PASARGUARD_ENV", env),
        patch("app.services.migrators.marzban.PASARGUARD_DATA", pg_data),
        patch("app.services.migrators.marzban.BACKUP_DIR", bak),
        patch("app.services.marzban_core_sync.PASARGUARD_ENV", env),
        patch("app.services.marzban_core_sync.PASARGUARD_DATA", pg_data),
    ):
        import asyncio

        snapshot = (
            'SQLALCHEMY_DATABASE_URL="mysql+pymysql://u:p@127.0.0.1/pasarguard"\n'
            'XRAY_JSON="./xray_config.json"\n'
        )
        asyncio.run(m._force_env_sqlite(snapshot))
        text = env.read_text(encoding="utf-8")
        assert "XRAY_JSON" in text
        assert "/var/lib/pasarguard/xray_config.json" in text
        assert 'XRAY_JSON="./xray_config.json"' not in text or text.count("XRAY_JSON") == 1
        # Absolute pin must win
        from app.services.env_migration import read_env_var

        assert read_env_var(text, "XRAY_JSON") == "/var/lib/pasarguard/xray_config.json"


def test_sync_replaces_default_core_and_inserts_marzban_inbounds(tmp_path):
    from app.services.marzban_core_sync import (
        assert_sqlite_core_matches_xray,
        sync_core_from_xray_sqlite,
    )

    db = tmp_path / "db.sqlite3"
    xray = tmp_path / "xray_config.json"
    _sqlite_with_default_core(db)
    xray.write_text(_marzban_xray(), encoding="utf-8")

    # Precondition: only default inbound
    conn = sqlite3.connect(str(db))
    tags_before = [r[0] for r in conn.execute("SELECT tag FROM inbounds").fetchall()]
    core_before = conn.execute("SELECT config FROM core_configs").fetchone()[0]
    conn.close()
    assert tags_before == ["Shadowsocks TCP"]
    assert "VLESS-Reality" not in core_before

    stats = sync_core_from_xray_sqlite(db, xray)
    assert stats["cores_updated"] == 1
    assert stats["inbounds_inserted"] >= 3

    assert_sqlite_core_matches_xray(db, xray)

    conn = sqlite3.connect(str(db))
    tags = {r[0] for r in conn.execute("SELECT tag FROM inbounds").fetchall()}
    core = conn.execute("SELECT config FROM core_configs").fetchone()[0]
    conn.close()
    assert "VLESS-Reality" in tags
    assert "VMess-WS" in tags
    assert "Shadowsocks" in tags
    assert "VLESS-Reality" in core
    assert "Shadowsocks TCP" in tags  # old default row may remain; Marzban tags must exist


def test_assert_detects_desynced_default_core(tmp_path):
    from app.services.marzban_core_sync import assert_sqlite_core_matches_xray

    db = tmp_path / "db.sqlite3"
    xray = tmp_path / "xray_config.json"
    _sqlite_with_default_core(db)
    xray.write_text(_marzban_xray(), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing inbound tags|not replaced|missing tags"):
        assert_sqlite_core_matches_xray(db, xray)


def test_require_xray_rejects_missing_and_untagged(tmp_path):
    from app.services.marzban_core_sync import require_marzban_xray_on_disk

    with patch("app.services.marzban_core_sync.PASARGUARD_DATA", tmp_path):
        with pytest.raises(RuntimeError, match="missing"):
            require_marzban_xray_on_disk()
        (tmp_path / "xray_config.json").write_text(
            json.dumps({"log": {"loglevel": "warning"}, "inbounds": [{}, {"port": 1}], "outbounds": []}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="no tagged inbounds"):
            require_marzban_xray_on_disk()
        (tmp_path / "xray_config.json").write_text(_marzban_xray(), encoding="utf-8")
        assert require_marzban_xray_on_disk().name == "xray_config.json"


def test_xray_copy_failure_is_hard_error(tmp_path):
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    src = tmp_path / "mz"
    src.mkdir()
    # Unreadable path simulation via transform failure: write invalid then patch transform
    (src / "xray_config.json").write_text("{not-json", encoding="utf-8")
    pg_data = tmp_path / "pgdata"
    pg_data.mkdir()
    env = tmp_path / ".env"
    env.write_text("XRAY_JSON=./xray_config.json\n", encoding="utf-8")

    m = MarzbanMigrator(MigrationJob(job_id="copyfail"), {})
    with (
        patch("app.services.migrators.marzban.PASARGUARD_DATA", pg_data),
        patch("app.services.migrators.marzban.PASARGUARD_ENV", env),
        patch(
            "app.services.migrators.marzban.transform_xray_config",
            side_effect=RuntimeError("boom"),
        ),
    ):
        import asyncio

        with pytest.raises(RuntimeError, match="Failed to copy Marzban xray_config"):
            asyncio.run(m._copy_marzban_assets(src))


def test_assert_ready_syncs_core_automatically(tmp_path):
    """_assert_sqlite_pasarguard_ready must heal desynced default core from disk xray."""
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    db = tmp_path / "db.sqlite3"
    pg_data = tmp_path / "data"
    pg_data.mkdir()
    _sqlite_with_default_core(db)
    (pg_data / "xray_config.json").write_text(_marzban_xray(), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("XRAY_JSON=/var/lib/pasarguard/xray_config.json\n", encoding="utf-8")

    m = MarzbanMigrator(MigrationJob(job_id="ready"), {})
    with (
        patch("app.services.migrators.marzban.PASARGUARD_DATA", pg_data),
        patch("app.services.migrators.marzban.PASARGUARD_ENV", env),
        patch("app.services.marzban_core_sync.PASARGUARD_DATA", pg_data),
        patch("app.services.marzban_core_sync.PASARGUARD_ENV", env),
    ):
        m._assert_sqlite_pasarguard_ready(db)

    conn = sqlite3.connect(str(db))
    tags = {r[0] for r in conn.execute("SELECT tag FROM inbounds").fetchall()}
    core = conn.execute("SELECT config FROM core_configs").fetchone()[0]
    conn.close()
    assert "VLESS-Reality" in tags
    assert "VLESS-Reality" in core
