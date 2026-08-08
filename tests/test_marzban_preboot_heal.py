"""Tests for Marzban pre-boot orphan FK + unique-name hygiene."""

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.marzban_preboot_heal import (
    cleanup_orphans_sqlite,
    logs_indicate_orphan_fk,
    orphan_delete_sql,
)


def test_orphan_delete_sql_shape():
    sql = orphan_delete_sql("node_usages", "node_id", "nodes", "id")
    assert "DELETE FROM node_usages" in sql
    assert "NOT EXISTS" in sql
    assert "nodes.id = node_usages.node_id" in sql
    print("OK: orphan delete sql")


def test_logs_indicate_orphan_fk():
    sample = (
        "sqlalchemy.exc.IntegrityError: (asyncmy.errors.IntegrityError) "
        "(1452, 'Cannot add or update a child row: a foreign key constraint fails "
        "(pasarguard.#sql-1_39, CONSTRAINT node_usages_ibfk_1 FOREIGN KEY (node_id) "
        "REFERENCES nodes (id))')"
    )
    assert logs_indicate_orphan_fk(sample) is True
    assert logs_indicate_orphan_fk("Duplicate entry 'usa-reality'") is False
    print("OK: orphan fk log detector")


def test_cleanup_orphans_sqlite_removes_only_orphans():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "db.sqlite3"
        db = sqlite3.connect(str(path))
        db.executescript(
            """
            CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE node_usages (id INTEGER PRIMARY KEY, node_id INTEGER);
            CREATE TABLE node_user_usages (
                id INTEGER PRIMARY KEY, node_id INTEGER, user_id INTEGER
            );
            CREATE TABLE hosts (id INTEGER PRIMARY KEY, inbound_tag TEXT);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT);

            INSERT INTO nodes VALUES (1, 'ok');
            INSERT INTO users VALUES (1, 'u1');
            INSERT INTO node_usages VALUES (1, 1);
            INSERT INTO node_usages VALUES (2, 999);  -- orphan
            INSERT INTO node_user_usages VALUES (1, 1, 1);
            INSERT INTO node_user_usages VALUES (2, 1, 888);  -- orphan user
            INSERT INTO inbounds VALUES (1, 'vless');
            INSERT INTO hosts VALUES (1, 'vless');
            INSERT INTO hosts VALUES (2, 'missing-tag');  -- orphan tag
            """
        )
        db.commit()
        db.close()

        deleted, nulled = cleanup_orphans_sqlite(path)
        assert deleted == 2
        assert nulled == 1

        db = sqlite3.connect(str(path))
        usage_ids = [r[0] for r in db.execute("SELECT id FROM node_usages").fetchall()]
        nuu = db.execute("SELECT id FROM node_user_usages").fetchall()
        host_tags = sorted(
            (r[0] if r[0] is not None else "")
            for r in db.execute("SELECT inbound_tag FROM hosts").fetchall()
        )
        db.close()
        assert usage_ids == [1]
        assert nuu == [(1,)]
        assert host_tags == ["", "vless"]

        # Clean second pass is a no-op
        assert cleanup_orphans_sqlite(path) == (0, 0)
    print("OK: sqlite orphan cleanup")


def test_cleanup_orphans_noop_on_clean_db():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "db.sqlite3"
        db = sqlite3.connect(str(path))
        db.executescript(
            """
            CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE node_usages (id INTEGER PRIMARY KEY, node_id INTEGER);
            INSERT INTO nodes VALUES (1, 'a');
            INSERT INTO node_usages VALUES (1, 1);
            """
        )
        db.commit()
        db.close()
        assert cleanup_orphans_sqlite(path) == (0, 0)
    print("OK: clean db noop")


if __name__ == "__main__":
    test_orphan_delete_sql_shape()
    test_logs_indicate_orphan_fk()
    test_cleanup_orphans_sqlite_removes_only_orphans()
    test_cleanup_orphans_noop_on_clean_db()
    print("\nAll marzban_preboot_heal tests passed.")
