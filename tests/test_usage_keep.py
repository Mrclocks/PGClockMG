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
    monkeypatch.setattr(usage_keep, "stash_and_clear_sqlite", lambda db, table: False)
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


def _count(path, table):
    db = sqlite3.connect(path)
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


def test_failed_stash_of_next_table_does_not_undo_previous_clear(tmp_path, monkeypatch):
    """Regression: a failing 2nd stash rolled back the 1st table's DELETE, leaving rows
    both live and in the side table — the restore then doubled them."""
    path = tmp_path / "db.sqlite3"
    _make_db(path, [(i, "x", 1, 1, i) for i in range(1, 101)])
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE node_usages (id INTEGER PRIMARY KEY, node_id INTEGER, used INTEGER)")
    db.executemany("INSERT INTO node_usages (node_id, used) VALUES (1, ?)", [(i,) for i in range(100)])
    db.commit()
    db.close()
    real = usage_keep.keep_name

    def flaky(table):
        if table == "node_usages":
            raise RuntimeError("disk full")
        return real(table)

    monkeypatch.setattr(usage_keep, "keep_name", flaky)
    failed: list = []
    out = shrink_heavy_usage_tables_sqlite(path, row_threshold=10, failed=failed)
    assert ("node_user_usages", 100) in out and failed == ["node_usages"]
    assert _count(path, "node_user_usages") == 0
    assert _count(path, "node_usages") == 100  # untouched
    monkeypatch.setattr(usage_keep, "keep_name", real)
    assert usage_keep.restore_sqlite(path) == [("node_user_usages", 100)]
    assert _count(path, "node_user_usages") == 100  # not 200


def test_failed_clear_leaves_no_side_copy(tmp_path):
    path = tmp_path / "db.sqlite3"
    _make_db(path, [(i, "x", 1, 1, i) for i in range(1, 21)])
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TRIGGER block_del BEFORE DELETE ON node_user_usages "
        "BEGIN SELECT RAISE(ABORT, 'locked'); END"
    )
    db.commit()
    db.close()
    failed: list = []
    assert shrink_heavy_usage_tables_sqlite(path, row_threshold=5, failed=failed) == []
    assert failed == ["node_user_usages"]
    assert _count(path, "node_user_usages") == 20
    db = sqlite3.connect(path)
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert "_pgkeep_node_user_usages" not in names  # copy rolled back with the delete
    assert usage_keep.restore_sqlite(path) == []
    assert _count(path, "node_user_usages") == 20


def test_restore_reports_skipped_rows(tmp_path):
    path = tmp_path / "db.sqlite3"
    _make_db(path, [(1, "x", 1, 1, 5), (2, "x", 99, 1, 5), (3, "x", 1, 77, 5)])
    shrink_heavy_usage_tables_sqlite(path, row_threshold=1)
    skipped: dict = {}
    assert usage_keep.restore_sqlite(path, None, skipped) == [("node_user_usages", 1)]
    assert skipped == {"node_user_usages": 2}


class _FakeMysqlCursor:
    """Just enough of a pymysql cursor for stash_and_clear_mysql."""

    def __init__(self, *, keep_exists, truncate_fails):
        self.keep_exists = keep_exists
        self.truncate_fails = truncate_fails
        self.sql: list[str] = []
        self._rows: list = []
        self.connection = type("C", (), {"commit": lambda self: None})()

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "information_schema.tables" in sql:
            self._rows = [(1,)] if (params[1].startswith("_pgkeep_") and self.keep_exists) or not params[1].startswith("_pgkeep_") else []
        elif "information_schema.columns" in sql:
            self._rows = [("id",), ("used",)] + ([("pgk_seq",)] if params[1].startswith("_pgkeep_") else [])
        elif "MAX(`pgk_seq`)" in sql:
            self._rows = [(41,)]
        elif sql.startswith("TRUNCATE") and self.truncate_fails:
            raise RuntimeError("Lock wait timeout exceeded")
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def test_mysql_truncate_failure_drops_new_side_table():
    cur = _FakeMysqlCursor(keep_exists=False, truncate_fails=True)
    assert usage_keep.stash_and_clear_mysql(cur, "node_user_usages", "db") is False
    assert any(s.startswith("CREATE TABLE `_pgkeep_node_user_usages`") for s in cur.sql)
    assert "DROP TABLE IF EXISTS `_pgkeep_node_user_usages`" in cur.sql


def test_mysql_truncate_failure_removes_only_appended_rows():
    cur = _FakeMysqlCursor(keep_exists=True, truncate_fails=True)
    assert usage_keep.stash_and_clear_mysql(cur, "node_user_usages", "db") is False
    assert "DELETE FROM `_pgkeep_node_user_usages` WHERE `pgk_seq` > %s" in cur.sql
    assert not any(s.startswith("DROP TABLE") for s in cur.sql)  # earlier history kept


def test_mysql_stash_and_clear_success():
    cur = _FakeMysqlCursor(keep_exists=False, truncate_fails=False)
    assert usage_keep.stash_and_clear_mysql(cur, "node_user_usages", "db") is True
    assert cur.sql[-1] == "TRUNCATE TABLE `node_user_usages`"
