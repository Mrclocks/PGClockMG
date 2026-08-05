"""Normalize modern 3x-ui multi-inbound clients schema for official migrator."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.services.migrators.xui import (
    _client_entry_for_protocol,
    detect_xui_db_schema,
    ensure_sudo_admin_from_xui,
    is_modern_xui_multi_inbound_schema,
    normalize_modern_xui_sqlite,
    sanitize_user_proxy_settings,
    seed_hosts_from_xui_inbounds,
    sync_user_groups_from_xui_settings,
)

MODERN_DB = Path(r"c:\Users\hrtag\Downloads\test1.mrclock.website_2026-08-02_104119.db")
LEGACY_DB = Path(r"c:\Users\hrtag\Downloads\x-ui (1).db")
LEGACY2_DB = Path(r"c:\Users\hrtag\Downloads\x-ui (2).db")


@pytest.mark.skipif(not MODERN_DB.is_file(), reason="modern sample db missing")
def test_detect_modern_schema():
    info = detect_xui_db_schema(MODERN_DB)
    assert info["schema"] == "modern"
    assert info["modern"] is True
    assert is_modern_xui_multi_inbound_schema(MODERN_DB) is True
    if LEGACY_DB.is_file():
        assert detect_xui_db_schema(LEGACY_DB)["schema"] == "legacy"
        assert is_modern_xui_multi_inbound_schema(LEGACY_DB) is False
    if LEGACY2_DB.is_file():
        assert detect_xui_db_schema(LEGACY2_DB)["schema"] == "legacy"
        assert is_modern_xui_multi_inbound_schema(LEGACY2_DB) is False


@pytest.mark.skipif(not MODERN_DB.is_file(), reason="modern sample db missing")
def test_normalize_rebuilds_settings_one_traffic_per_email():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "x-ui.db"
        shutil.copy2(MODERN_DB, work)

        before = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
        try:
            members = before.execute("SELECT COUNT(*) FROM client_inbounds").fetchone()[0]
            n_clients = before.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        finally:
            before.close()
        assert members == 6
        assert n_clients == 2

        stats = normalize_modern_xui_sqlite(work)
        assert stats["normalized"] is True
        assert stats["traffics_written"] == n_clients  # one row per email
        assert stats["memberships"] == members
        assert stats["inbounds_settings_updated"] == 3
        assert stats.get("one_traffic_per_email") is True

        conn = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
        try:
            n_after = conn.execute("SELECT COUNT(*) FROM client_traffics").fetchone()[0]
            assert n_after == n_clients
            emails = {
                r[0]
                for r in conn.execute("SELECT email FROM client_traffics")
            }
            assert emails == {"7didmnu1sz", "uubnxn4ska"}
            for iid, settings_json in conn.execute("SELECT id, settings FROM inbounds"):
                data = json.loads(settings_json)
                clients = data.get("clients") or []
                assert len(clients) == 2
                sub_ids = {c.get("subId") for c in clients}
                assert "lrd1ygi2lcfgqrgp" in sub_ids
                assert "d7la09bll1ue6qhi" in sub_ids
                if iid in (1, 2):  # vless
                    assert all(c.get("id") for c in clients)
                if iid == 3:  # trojan
                    assert all(c.get("password") for c in clients)
        finally:
            conn.close()


@pytest.mark.skipif(not MODERN_DB.is_file(), reason="modern sample db missing")
def test_sync_user_groups_fills_multi_inbound_membership():
    """Simulate official migrator's under-linked groups, then repair."""
    with tempfile.TemporaryDirectory() as tmp:
        xui = Path(tmp) / "x-ui.db"
        pg = Path(tmp) / "pg.db"
        shutil.copy2(MODERN_DB, xui)
        normalize_modern_xui_sqlite(xui)

        conn = sqlite3.connect(pg)
        conn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE);
            CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE inbounds_groups_association (
                inbound_id INTEGER, group_id INTEGER
            );
            CREATE TABLE users_groups_association (
                user_id INTEGER, groups_id INTEGER
            );
            INSERT INTO users VALUES (1, '7didmnu1sz'), (2, 'uubnxn4ska');
            INSERT INTO groups VALUES (10, 'g1'), (20, 'g2'), (30, 'g3');
            INSERT INTO inbounds_groups_association VALUES
                (1, 10), (2, 20), (3, 30);
            -- Official tool only linked each user to one group + orphan
            INSERT INTO users_groups_association VALUES
                (1, 10), (2, 10), (99, 20);
            """
        )
        conn.commit()
        conn.close()

        result = sync_user_groups_from_xui_settings(pg, xui)
        assert result["synced"] is True
        assert result["links_added"] == 4  # 2 users × 2 missing groups
        assert result["orphans_removed"] == 1

        conn = sqlite3.connect(f"file:{pg}?mode=ro", uri=True)
        try:
            for uid in (1, 2):
                n = conn.execute(
                    "SELECT COUNT(*) FROM users_groups_association WHERE user_id=?",
                    (uid,),
                ).fetchone()[0]
                assert n == 3
            orphans = conn.execute(
                """
                SELECT COUNT(*) FROM users_groups_association
                WHERE user_id NOT IN (SELECT id FROM users)
                """
            ).fetchone()[0]
            assert orphans == 0
        finally:
            conn.close()


def test_sanitize_short_shadowsocks_password_for_sub():
    """PasarGuard /sub 500: ShadowsocksSettings.password min_length=22."""
    with tempfile.TemporaryDirectory() as tmp:
        pg = Path(tmp) / "pg.db"
        conn = sqlite3.connect(pg)
        short = "nf58lsn78odoizm2"  # 16 chars from modern 3x-ui sample
        assert len(short) < 22
        ps = {
            "vmess": {"id": "f9d010f5-5812-487b-a2b4-3705b9f69dbb"},
            "vless": {"id": "f9d010f5-5812-487b-a2b4-3705b9f69dbb", "flow": ""},
            "trojan": {"password": short},
            "shadowsocks": {"password": short, "method": "chacha20-ietf-poly1305"},
        }
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, proxy_settings TEXT)"
        )
        conn.execute(
            "INSERT INTO users VALUES (1, '7didmnu1sz', ?)",
            (json.dumps(ps),),
        )
        conn.commit()
        conn.close()

        result = sanitize_user_proxy_settings(pg)
        assert result["fixed"] == 1

        conn = sqlite3.connect(f"file:{pg}?mode=ro", uri=True)
        raw = conn.execute("SELECT proxy_settings FROM users WHERE id=1").fetchone()[0]
        conn.close()
        out = json.loads(raw)
        assert len(out["shadowsocks"]["password"]) >= 22
        assert out["trojan"]["password"] == short  # trojan kept
        assert "flow" not in out["vless"]

        # Mimic PasarGuard ProxyTable shadowsocks constraint
        from pydantic import BaseModel, Field, ValidationError

        class SS(BaseModel):
            password: str = Field(min_length=22)
            method: str = "chacha20-ietf-poly1305"

        SS.model_validate(out["shadowsocks"])


@pytest.mark.skipif(not LEGACY_DB.is_file(), reason="legacy sample db missing")
def test_normalize_skips_legacy():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "x-ui.db"
        shutil.copy2(LEGACY_DB, work)
        stats = normalize_modern_xui_sqlite(work)
        assert stats["normalized"] is False


@pytest.mark.skipif(not MODERN_DB.is_file(), reason="modern sample db missing")
def test_seed_hosts_and_admin_for_modern():
    with tempfile.TemporaryDirectory() as tmp:
        xui = Path(tmp) / "x-ui.db"
        pg = Path(tmp) / "pg.db"
        shutil.copy2(MODERN_DB, xui)
        normalize_modern_xui_sqlite(xui)

        conn = sqlite3.connect(pg)
        conn.executescript(
            """
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);
            CREATE TABLE hosts (
                id INTEGER PRIMARY KEY,
                remark VARCHAR(256) NOT NULL,
                address VARCHAR(256) NOT NULL,
                port INTEGER,
                inbound_tag VARCHAR(256),
                sni VARCHAR(1000),
                host VARCHAR(1000),
                security VARCHAR(15) DEFAULT 'inbound_default' NOT NULL,
                alpn VARCHAR(14) DEFAULT '',
                fingerprint VARCHAR(16) DEFAULT 'none' NOT NULL,
                allowinsecure BOOLEAN,
                is_disabled BOOLEAN,
                path VARCHAR(256),
                random_user_agent BOOLEAN DEFAULT '0' NOT NULL,
                use_sni_as_host BOOLEAN DEFAULT '0' NOT NULL,
                priority INTEGER NOT NULL,
                status VARCHAR(60) DEFAULT ''
            );
            CREATE TABLE admins (
                id INTEGER PRIMARY KEY,
                username VARCHAR(34) NOT NULL,
                hashed_password VARCHAR(128) NOT NULL,
                created_at DATETIME NOT NULL,
                is_sudo BOOLEAN DEFAULT 0 NOT NULL,
                used_traffic BIGINT DEFAULT 0 NOT NULL,
                is_disabled BOOLEAN DEFAULT 0 NOT NULL
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, username TEXT, admin_id INTEGER
            );
            INSERT INTO inbounds VALUES
                (1, 'in-45361-tcp'), (2, 'in-58551-tcp'), (3, 'in-56038-tcp');
            INSERT INTO users VALUES (1, '7didmnu1sz', 1), (2, 'uubnxn4ska', 1);
            """
        )
        conn.commit()
        conn.close()

        hosts = seed_hosts_from_xui_inbounds(
            pg, xui, share_address="test1.mrclock.website",
        )
        assert hosts["seeded"] == 3
        assert hosts["address"] == "test1.mrclock.website"

        admin = ensure_sudo_admin_from_xui(pg, xui)
        assert admin["created"] is True

        conn = sqlite3.connect(f"file:{pg}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] == 3
            for addr, tag, alpn in conn.execute(
                "SELECT address, inbound_tag, alpn FROM hosts"
            ):
                assert addr == "test1.mrclock.website"
                assert tag.startswith("in-")
                assert alpn != "none"  # EnumArray-breaking value
            assert conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 1
        finally:
            conn.close()


def test_client_entry_ignores_integer_pk_as_uuid():
    """Modern clients.id is int PK — must not call .strip() on it (regression)."""
    row = {
        "id": 7,
        "uuid": None,
        "email": "user1",
        "sub_id": "abc123",
        "enable": 1,
        "expiry_time": 0,
        "total_gb": 0,
        "limit_ip": 0,
        "password": "",
        "flow": "",
    }
    entry = _client_entry_for_protocol("vless", row)
    assert entry["email"] == "user1"
    assert "id" not in entry  # no uuid available; int PK must not become id

    row["uuid"] = "f9d010f5-5812-487b-a2b4-3705b9f69dbb"
    entry = _client_entry_for_protocol("vless", row)
    assert entry["id"] == "f9d010f5-5812-487b-a2b4-3705b9f69dbb"

    # Classic settings.clients[] uses string id as UUID
    classic = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "email": "c1"}
    assert _client_entry_for_protocol("vmess", classic)["id"] == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )


def test_normalize_modern_with_null_uuid_does_not_crash():
    """Reproduce AttributeError: 'int' object has no attribute 'strip'."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "x-ui.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY,
                email TEXT,
                uuid TEXT,
                sub_id TEXT,
                enable INTEGER DEFAULT 1,
                expiry_time INTEGER DEFAULT 0,
                total_gb INTEGER DEFAULT 0,
                limit_ip INTEGER DEFAULT 0,
                password TEXT,
                flow TEXT,
                tg_id TEXT,
                comment TEXT,
                reset INTEGER DEFAULT 0,
                security TEXT
            );
            CREATE TABLE client_inbounds (
                id INTEGER PRIMARY KEY,
                client_id INTEGER,
                inbound_id INTEGER,
                flow_override TEXT
            );
            CREATE TABLE client_traffics (
                id INTEGER PRIMARY KEY,
                inbound_id INTEGER,
                enable NUMERIC,
                email TEXT UNIQUE,
                up INTEGER, down INTEGER,
                expiry_time INTEGER, total INTEGER,
                reset INTEGER DEFAULT 0
            );
            CREATE TABLE inbounds (
                id INTEGER PRIMARY KEY,
                protocol TEXT,
                settings TEXT
            );
            INSERT INTO clients (id, email, uuid, sub_id, enable)
            VALUES (1, 'u1', NULL, 'sub1', 1),
                   (2, 'u2', '', 'sub2', 1),
                   (3, 'u3', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'sub3', 1);
            INSERT INTO client_inbounds (client_id, inbound_id) VALUES
                (1, 1), (2, 1), (3, 1);
            INSERT INTO client_traffics (inbound_id, enable, email, up, down, expiry_time, total)
            VALUES (1, 1, 'u1', 0, 0, 0, 0),
                   (1, 1, 'u2', 0, 0, 0, 0),
                   (1, 1, 'u3', 0, 0, 0, 0);
            INSERT INTO inbounds (id, protocol, settings)
            VALUES (1, 'vless', '{"clients":[]}');
            """
        )
        conn.commit()
        conn.close()

        stats = normalize_modern_xui_sqlite(db)
        assert stats["normalized"] is True

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            settings = json.loads(
                conn.execute("SELECT settings FROM inbounds WHERE id=1").fetchone()[0]
            )
            clients = settings["clients"]
            assert len(clients) == 3
            by_email = {c["email"]: c for c in clients}
            assert "id" not in by_email["u1"]
            assert "id" not in by_email["u2"]
            assert by_email["u3"]["id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        finally:
            conn.close()
