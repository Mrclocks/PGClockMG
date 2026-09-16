"""Tests for hosts COPY sanitize / recover helpers (same-engine PG restore)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.hosts_dump_sanitize import (
    count_hosts_rows_in_pg_dump,
    extract_sanitized_hosts_copy_sql,
    sanitize_hosts_copy_in_pg_dump,
    sanitize_hosts_copy_row,
)


_HOST_COLS = (
    "id, remark, address, port, inbound_tag, sni, host, security, fingerprint, "
    "allowinsecure, random_user_agent, use_sni_as_host, is_disabled, priority"
)


def _sample_dump(*, address: str = "{cdn.example.com}", inbound: str = "VLESS-TCP") -> str:
    return (
        "-- PostgreSQL database dump\n"
        "COPY public.users (id, username) FROM stdin;\n"
        "1\talice\n"
        "\\.\n"
        f"COPY public.hosts ({_HOST_COLS}) FROM stdin;\n"
        f"1\tmyhost\t{address}\t443\t{inbound}\t\\N\t\\N\tinbound_default\tnone\t"
        "f\tf\tf\tf\t0\n"
        f"2\tother\t\\ufeff example.org\t8443\t{inbound}\t\\N\t\\N\tinbound_default\tnone\t"
        "t\tf\tf\tf\t1\n"
        "\\.\n"
        "COPY public.inbounds (id, tag) FROM stdin;\n"
        "1\tVLESS-TCP\n"
        "\\.\n"
    )


def test_sanitize_hosts_copy_row_unwraps_address_and_bools():
    cols = [c.strip() for c in _HOST_COLS.split(",")]
    raw = (
        "1\tmyhost\t{cdn.example.com}\t443\tVLESS-TCP\t\\N\t\\N\tinbound_default\tnone\t"
        "f\tf\tf\tf\t0"
    )
    out = sanitize_hosts_copy_row(cols, raw)
    fields = out.split("\t")
    assert fields[2] == "cdn.example.com"
    assert fields[9] == "f"
    assert fields[10] == "f"
    print("OK: sanitize_hosts_copy_row_unwraps_address_and_bools")


def test_sanitize_hosts_copy_in_pg_dump_rewrites_only_hosts():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "dump.sql"
        dest = Path(td) / "out.sql"
        # Use real BOM/ZWSP junk that convert_value strips
        src.write_text(
            _sample_dump(address="{cdn.example.com}"),
            encoding="utf-8",
        )
        stats = sanitize_hosts_copy_in_pg_dump(src, dest)
        assert stats["rows"] == 2
        text = dest.read_text(encoding="utf-8")
        assert "COPY public.users" in text
        assert "alice" in text
        assert "{cdn.example.com}" not in text
        assert "cdn.example.com" in text
        assert "COPY public.inbounds" in text
        assert count_hosts_rows_in_pg_dump(src) == 2
    print("OK: sanitize_hosts_copy_in_pg_dump_rewrites_only_hosts")


def test_extract_sanitized_hosts_copy_sql_standalone_block():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "dump.sql"
        src.write_text(_sample_dump(address='{"edge.example.com"}'), encoding="utf-8")
        block = extract_sanitized_hosts_copy_sql(src)
        assert block is not None
        assert block.startswith("COPY public.hosts (")
        assert "edge.example.com" in block
        assert '{"edge.example.com"}' not in block
        assert block.rstrip().endswith("\\.")
        assert block.count("\n") >= 3
    print("OK: extract_sanitized_hosts_copy_sql_standalone_block")


def test_sanitize_preserves_clean_hostname():
    cols = [c.strip() for c in _HOST_COLS.split(",")]
    raw = (
        "9\tok\tclean.example.com\t443\ttag\t\\N\t\\N\tinbound_default\tnone\t"
        "f\tf\tf\tf\t0"
    )
    out = sanitize_hosts_copy_row(cols, raw)
    assert out.split("\t")[2] == "clean.example.com"
    print("OK: sanitize_preserves_clean_hostname")


def test_recover_helper_wired_in_pg_restore():
    from app.services import pg_restore as mod

    assert callable(getattr(mod, "_recover_hosts_if_missing", None))
    print("OK: recover_helper_wired_in_pg_restore")


if __name__ == "__main__":
    test_sanitize_hosts_copy_row_unwraps_address_and_bools()
    test_sanitize_hosts_copy_in_pg_dump_rewrites_only_hosts()
    test_extract_sanitized_hosts_copy_sql_standalone_block()
    test_sanitize_preserves_clean_hostname()
    test_recover_helper_wired_in_pg_restore()
    print("ALL PASSED")
