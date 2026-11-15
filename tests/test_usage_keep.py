"""Usage history is set aside before the panel upgrade and restored after."""

import sqlite3

from app.services import usage_keep
from app.services.marzban_preboot_heal import (
    restore_usage_tables_on_conn,
    shrink_heavy_usage_tables_sqlite,
)


def _make_db(path, rows):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE node_user_usages (
            id INTEGER PRIMARY KEY, created_at TEXT, user_id INTEGER, node_id INTEGER, used_traffic INTEGER);
        INSERT INTO users VALUES (1,'a'),(2,'b');
        INSERT INTO nodes VALUES (1,'n1');
        """
    )
    db.executemany(
        "INSERT INTO node_user_usages (id, created_at, user_id, node_id, used_traffic) VALUES (?,?,?,?,?)",
        rows,
    )
    db.commit()
    db.close()


def test_stash_then_restore_roundtrip(tmp_path):
    path = tmp_path / "db.sqlite3"
    rows = [(i, "2025-01-01", 1 + i % 2, 1, i) for i in range(1, 61)]
    _make_db(path, rows)

    truncated = shrink_heavy_usage_tables_sqlite(path, row_threshold=50)
    assert truncated == [("node_user_usages", 60)]
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM node_user_usages").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM _pgkeep_node_user_usages").fetchone()[0] == 60
    # simulate the upgraded table gaining a column
    db.execute("ALTER TABLE node_user_usages ADD COLUMN extra TEXT")
    db.commit()
    db.close()

    restored = restore_usage_tables_on_conn("sqlite", {"sqlite_path": str(path)})
    assert restored == [("node_user_usages", 60)]
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM node_user_usages").fetchone()[0] == 60
    # rows come back WITHOUT their old ids (new ids never collide with rows the panel wrote)
    assert db.execute("SELECT COUNT(DISTINCT id) FROM node_user_usages").fetchone()[0] == 60
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "_pgkeep_node_user_usages" not in names
    db.close()


def test_restore_skips_rows_with_missing_parents(tmp_path):
    path = tmp_path / "db.sqlite3"
    rows = [(1, "x", 1, 1, 5), (2, "x", 99, 1, 5), (3, "x", 1, 77, 5)]
    _make_db(path, rows)
    shrink_heavy_usage_tables_sqlite(path, row_threshold=1)
    assert usage_keep.restore_sqlite(path) == [("node_user_usages", 1)]


def test_restore_noop_without_stash(tmp_path):
    path = tmp_path / "db.sqlite3"
    _make_db(path, [(1, "x", 1, 1, 5)])
    assert usage_keep.restore_sqlite(path) == []
    assert restore_usage_tables_on_conn("postgresql", {}) == []


def test_table_untouched_when_stash_fails(tmp_path, monkeypatch):
    from app.services import usage_keep

    path = tmp_path / "db.sqlite3"
    _make_db(path, [(i, "x", 1, 1, i) for i in range(1, 11)])
    monkeypatch.setattr(usage_keep, "stash_sqlite", lambda db, table: False)
    failed: list = []
    out = shrink_heavy_usage_tables_sqlite(path, row_threshold=5, failed=failed)
    assert out == [] and failed == ["node_user_usages"]
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM node_user_usages").fetchone()[0] == 10
    db.close()


def test_restore_does_not_collide_with_rows_written_meanwhile(tmp_path):
    path = tmp_path / "db.sqlite3"
    _make_db(path, [(i, "old", 1, 1, i) for i in range(1, 6)])
    shrink_heavy_usage_tables_sqlite(path, row_threshold=1)
    db = sqlite3.connect(path)
    # the running panel already wrote new rows reusing ids 1..3
    db.executemany(
        "INSERT INTO node_user_usages (id, created_at, user_id, node_id, used_traffic) VALUES (?,?,?,?,?)",
        [(i, "new", 1, 1, 100 + i) for i in range(1, 4)],
    )
    db.commit()
    db.close()
    assert usage_keep.restore_sqlite(path) == [("node_user_usages", 5)]
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM node_user_usages").fetchone()[0] == 8
    assert db.execute("SELECT COUNT(*) FROM node_user_usages WHERE created_at='old'").fetchone()[0] == 5
    db.close()


def test_restore_resumes_without_duplicates_and_keeps_leftover_side_table(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite3"
    _make_db(path, [(i, "x", 1, 1, i) for i in range(1, 31)])
    shrink_heavy_usage_tables_sqlite(path, row_threshold=1)
    # second run (e.g. retry) must append to, not drop, an existing side table
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO node_user_usages (id, created_at, user_id, node_id, used_traffic) VALUES (999,'y',1,1,1)"
    )
    db.commit()
    db.close()
    shrink_heavy_usage_tables_sqlite(path, row_threshold=1)
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM _pgkeep_node_user_usages").fetchone()[0] == 31
    db.close()
    monkeypatch.setattr(usage_keep, "CHUNK_ROWS", 7)
    failures: list = []
    assert usage_keep.restore_sqlite(path, failures) == [("node_user_usages", 31)]
    assert failures == []


def test_restore_failure_is_reported(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE _pgkeep_node_user_usages (id INTEGER, user_id INTEGER)")
    db.commit()
    db.close()  # target table node_user_usages does not exist
    failures: list = []
    assert usage_keep.restore_sqlite(path, failures) == []
    assert failures and failures[0][0] == "node_user_usages"
