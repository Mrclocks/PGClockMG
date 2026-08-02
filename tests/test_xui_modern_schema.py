"""Normalize modern 3x-ui multi-inbound clients schema for official migrator."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.services.migrators.xui import (
    is_modern_xui_multi_inbound_schema,
    normalize_modern_xui_sqlite,
    sync_user_groups_from_xui_settings,
)

MODERN_DB = Path(r"c:\Users\hrtag\Downloads\test1.mrclock.website_2026-08-02_104119.db")
LEGACY_DB = Path(r"c:\Users\hrtag\Downloads\x-ui (1).db")
LEGACY2_DB = Path(r"c:\Users\hrtag\Downloads\x-ui (2).db")


@pytest.mark.skipif(not MODERN_DB.is_file(), reason="modern sample db missing")
def test_detect_modern_schema():
    assert is_modern_xui_multi_inbound_schema(MODERN_DB) is True
    if LEGACY_DB.is_file():
        assert is_modern_xui_multi_inbound_schema(LEGACY_DB) is False
    if LEGACY2_DB.is_file():
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


@pytest.mark.skipif(not LEGACY_DB.is_file(), reason="legacy sample db missing")
def test_normalize_skips_legacy():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "x-ui.db"
        shutil.copy2(LEGACY_DB, work)
        stats = normalize_modern_xui_sqlite(work)
        assert stats["normalized"] is False
