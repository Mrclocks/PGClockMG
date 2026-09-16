"""Tests for permanent hosts dump extract / strip / INSERT rebuild."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.hosts_dump_sanitize import (
    build_hosts_insert_sql,
    count_hosts_rows_in_pg_dump,
    extract_hosts_rows_from_pg_dump,
    find_hosts_dump,
    sanitize_hosts_copy_row,
    strip_hosts_copy_data_from_pg_dump,
)


_CREATE = """CREATE TABLE public.hosts (
    id integer NOT NULL,
    remark character varying NOT NULL,
    address character varying NOT NULL,
    port integer,
    inbound_tag character varying,
    sni character varying,
    host character varying,
    security character varying,
    fingerprint character varying,
    allowinsecure boolean,
    random_user_agent boolean,
    use_sni_as_host boolean,
    is_disabled boolean,
    priority integer
);
"""

_COLS = (
    "id, remark, address, port, inbound_tag, sni, host, security, fingerprint, "
    "allowinsecure, random_user_agent, use_sni_as_host, is_disabled, priority"
)


def _copy_dump(*, address: str = "{cdn.example.com}", with_cols: bool = True) -> str:
    head = (
        f"COPY public.hosts ({_COLS}) FROM stdin;"
        if with_cols
        else "COPY public.hosts FROM stdin;"
    )
    return (
        "-- PostgreSQL database dump\n"
        + _CREATE
        + "COPY public.users (id, username) FROM stdin;\n"
        "1\talice\n"
        "\\.\n"
        f"{head}\n"
        f"1\tmyhost\t{address}\t443\tVLESS-TCP\t\\N\t\\N\tinbound_default\tnone\t"
        "f\tf\tf\tf\t0\n"
        "2\tother\texample.org\t8443\tmissing-tag\t\\N\t\\N\tinbound_default\tnone\t"
        "t\tf\tf\tf\t1\n"
        "\\.\n"
        "COPY public.inbounds (id, tag) FROM stdin;\n"
        "1\tVLESS-TCP\n"
        "\\.\n"
    )


def test_extract_copy_with_and_without_column_list():
    with tempfile.TemporaryDirectory() as td:
        p1 = Path(td) / "with.sql"
        p1.write_text(_copy_dump(with_cols=True), encoding="utf-8")
        cols, rows = extract_hosts_rows_from_pg_dump(p1)
        assert len(rows) == 2
        assert rows[0]["address"] == "cdn.example.com"
        assert rows[0]["allowinsecure"] is False

        p2 = Path(td) / "without.sql"
        p2.write_text(_copy_dump(with_cols=False), encoding="utf-8")
        cols2, rows2 = extract_hosts_rows_from_pg_dump(p2)
        assert len(rows2) == 2, "COPY without column list must use CREATE TABLE order"
        assert rows2[0]["address"] == "cdn.example.com"
        assert "id" in cols2
    print("OK: extract_copy_with_and_without_column_list")


def test_strip_hosts_keeps_other_tables():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "dump.sql"
        dest = Path(td) / "out.sql"
        src.write_text(_copy_dump(), encoding="utf-8")
        n = strip_hosts_copy_data_from_pg_dump(src, dest)
        assert n == 2
        text = dest.read_text(encoding="utf-8")
        assert "alice" in text
        assert "VLESS-TCP" in text
        assert "cdn.example.com" not in text
        assert "COPY public.hosts" in text  # header kept
        assert count_hosts_rows_in_pg_dump(dest) == 0
        assert count_hosts_rows_in_pg_dump(src) == 2
    print("OK: strip_hosts_keeps_other_tables")


def test_build_insert_retargets_inbound_tag():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "dump.sql"
        src.write_text(_copy_dump(), encoding="utf-8")
        _cols, rows = extract_hosts_rows_from_pg_dump(src)
        sql = build_hosts_insert_sql(
            rows,
            ["id", "remark", "address", "port", "inbound_tag", "security",
             "fingerprint", "allowinsecure", "priority"],
            inbound_tags=["VLESS-TCP"],
        )
        assert sql is not None
        assert "DELETE FROM public.hosts" in sql
        assert "cdn.example.com" in sql
        assert "missing-tag" not in sql  # retargeted to VLESS-TCP
        assert sql.count("VLESS-TCP") >= 2
    print("OK: build_insert_retargets_inbound_tag")


def test_find_hosts_dump_scans_candidates():
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "empty.sql"
        empty.write_text("COPY public.users (id) FROM stdin;\n1\n\\.\n", encoding="utf-8")
        full = Path(td) / "full.sql"
        full.write_text(_copy_dump(), encoding="utf-8")
        assert find_hosts_dump([empty, full]) == full
        assert find_hosts_dump([empty]) is None
    print("OK: find_hosts_dump_scans_candidates")


def test_sanitize_hosts_copy_row_bools():
    cols = [c.strip() for c in _COLS.split(",")]
    raw = (
        "1\tmyhost\t{cdn.example.com}\t443\tVLESS-TCP\t\\N\t\\N\tinbound_default\tnone\t"
        "f\tf\tf\tf\t0"
    )
    out = sanitize_hosts_copy_row(cols, raw)
    assert out.split("\t")[2] == "cdn.example.com"
    assert out.split("\t")[9] == "f"
    print("OK: sanitize_hosts_copy_row_bools")


def test_recover_helper_wired():
    from app.services import pg_restore as mod

    assert callable(getattr(mod, "_recover_hosts_if_missing", None))
    print("OK: recover_helper_wired")


if __name__ == "__main__":
    test_extract_copy_with_and_without_column_list()
    test_strip_hosts_keeps_other_tables()
    test_build_insert_retargets_inbound_tag()
    test_find_hosts_dump_scans_candidates()
    test_sanitize_hosts_copy_row_bools()
    test_recover_helper_wired()
    print("ALL PASSED")
