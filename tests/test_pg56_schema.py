"""PasarGuard v5.6 schema additions: passkey binary columns and new tables."""

import os
import sqlite3

from app.services.native_migration.adapters import create_reader, create_writer, copy_tables_universal
from app.services.native_migration.copy_core import TABLE_ORDER, convert_value


def test_binary_columns_stay_bytes():
    raw = bytes(range(1, 40)) + b"\xff\xfe\x00"
    assert convert_value("admin_passkeys", "credential_id", raw) == raw
    assert convert_value("admin_passkeys", "public_key", memoryview(raw)) == raw
    assert convert_value("admin_passkeys", "public_key", None) is None
    # other tables keep the old text normalisation
    assert convert_value("users", "note", b"hello") == "hello"


def test_new_v56_tables_are_in_copy_order():
    for t in ("admin_passkeys", "node_outbound_usages", "wireguard_subnets"):
        assert t in TABLE_ORDER
    from app.services.native_migration.copy_core import SKIP_TABLES

    assert "passkey_challenges" not in TABLE_ORDER and "passkey_challenges" in SKIP_TABLES
    assert TABLE_ORDER.index("admins") < TABLE_ORDER.index("admin_passkeys")
    assert TABLE_ORDER.index("nodes") < TABLE_ORDER.index("node_outbound_usages")


def test_passkey_bytes_survive_sqlite_copy(tmp_path):
    cred = os.urandom(32) + b"\xa3\x9c\xff"
    pub = os.urandom(77)
    src, dst = str(tmp_path / "src.db"), str(tmp_path / "dst.db")
    schema = """
        CREATE TABLE admins (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE admin_passkeys (
            id INTEGER PRIMARY KEY, admin_id INTEGER, credential_id BLOB, public_key BLOB,
            sign_count INTEGER, name TEXT);
    """
    for path in (src, dst):
        db = sqlite3.connect(path)
        db.executescript(schema)
        db.commit()
        db.close()
    db = sqlite3.connect(src)
    db.execute("INSERT INTO admins VALUES (1, 'root')")
    db.execute("INSERT INTO admin_passkeys VALUES (1, 1, ?, ?, 5, 'phone')", (cred, pub))
    db.commit()
    db.close()

    reader = create_reader("sqlite", src, {})
    writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
    try:
        stats, _ = copy_tables_universal(reader, writer, lambda m: None, fail_hard=False)
    finally:
        reader.close()
        writer.close()

    assert stats.get("admin_passkeys") == 1
    db = sqlite3.connect(dst)
    got = db.execute("SELECT credential_id, public_key, name FROM admin_passkeys").fetchone()
    db.close()
    assert got[0] == cred and got[1] == pub and got[2] == "phone"


def test_binary_columns_come_from_the_target_schema(tmp_path):
    """A BLOB column we have never heard of must also stay raw bytes."""
    from app.services.native_migration.copy_core import is_binary_column

    src, dst = str(tmp_path / "s.db"), str(tmp_path / "d.db")
    schema = "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name TEXT, secret_blob BLOB);"
    for path in (src, dst):
        db = sqlite3.connect(path)
        db.executescript(schema)
        db.commit()
        db.close()
    blob = bytes([0xEF, 0xBB, 0xBF, 0x61, 0xFF, 0x00, 0x80])
    db = sqlite3.connect(src)
    db.execute("INSERT INTO api_keys VALUES (1, 'k', ?)", (blob,))
    db.commit()
    db.close()

    reader = create_reader("sqlite", src, {})
    writer = create_writer("sqlite", {"sqlite_path": dst}, dst)
    try:
        assert writer.binary_columns("api_keys") == ["secret_blob"]
        copy_tables_universal(reader, writer, lambda m: None, fail_hard=False)
    finally:
        reader.close()
        writer.close()
    assert is_binary_column("api_keys", "secret_blob")
    db = sqlite3.connect(dst)
    assert db.execute("SELECT secret_blob FROM api_keys").fetchone()[0] == blob
    db.close()


def test_postgres_writer_reports_bytea_and_skips_text_copy():
    from app.services.native_migration.adapters import PostgresWriter

    w = PostgresWriter.__new__(PostgresWriter)
    w._col_types = {"things": {"id": "integer", "blob": "bytea"}}
    w._col_nullable = {"things": {}}
    assert w.binary_columns("things") == ["blob"]
    w._bulk_load_active = True
    w._batch_buf, w._batch_table, w._batch_columns, w._value_clears = [], None, None, []
    # a bytea table must not be buffered for COPY
    w._coerce_row = lambda t, c, v: v
    class Cur:
        def execute(self, *a, **k): pass
    class Conn:
        def cursor(self): return Cur()
    w._conn = Conn()
    class P:
        def SQL(self, *a, **k):
            class X:
                def format(self, *a, **k): return self
                def join(self, *a, **k): return self
            return X()
        Identifier = Placeholder = staticmethod(lambda *a, **k: None)
    w._psql = P()
    w.insert("things", ["id", "blob"], (1, b"\xff"))
    assert w._batch_buf == []
