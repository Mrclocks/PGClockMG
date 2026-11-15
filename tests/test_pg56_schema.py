"""PasarGuard v5.6 schema additions: passkey binary columns and new tables."""

import os
import sqlite3

from app.services.native_migration.adapters import create_reader, create_writer, copy_tables_universal
from app.services.native_migration.copy_core import TABLE_ORDER, convert_value


def test_binary_columns_stay_bytes():
    raw = bytes(range(1, 40)) + b"\xff\xfe\x00"
    assert convert_value("admin_passkeys", "credential_id", raw) == raw
    assert convert_value("admin_passkeys", "public_key", memoryview(raw)) == raw
    assert convert_value("passkey_challenges", "challenge", b"\xef\xbb\xbfabc") == b"\xef\xbb\xbfabc"
    assert convert_value("admin_passkeys", "public_key", None) is None
    # other tables keep the old text normalisation
    assert convert_value("users", "note", b"hello") == "hello"


def test_new_v56_tables_are_in_copy_order():
    for t in ("admin_passkeys", "passkey_challenges", "node_outbound_usages", "wireguard_subnets"):
        assert t in TABLE_ORDER
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
