"""Backup cleanup: dump filtering, SQLite shrink and archive-level behaviour."""

from __future__ import annotations

import sqlite3
import sys
import zipfile
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backup_cleanup import (  # noqa: E402
    CLEANABLE_TABLES,
    CLEANUP_RULES,
    RULES_BY_ID,
    FilterStats,
    _count_value_tuples,
    _statement_is_complete,
    analyze_cleanup,
    apply_cleanup,
    clean_sqlite,
    default_rule_ids,
    filter_sql_stream,
    measure_sqlite,
    resolve_tables,
)

ENV_TEXT = (
    "SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://pg:pw@127.0.0.1:5432/pasarguard\n"
    "DB_PASSWORD=pw\n"
)


def _filter(text: str, tables: set[str]) -> tuple[str, FilterStats]:
    out = StringIO()
    stats = filter_sql_stream(StringIO(text), out, tables)
    return out.getvalue(), stats


# ---------------------------------------------------------------- guardrails


def test_cleanable_tables_are_never_critical():
    """A rule must never target a table the restore verifies or aborts on."""
    from app.services.native_migration.copy_core import (
        MIGRATION_ABORT_IF_ZERO,
        STRICT_COMPLETE_TABLES,
        SUBSCRIPTION_TABLES,
        VERIFY_TABLES,
    )

    for name, critical in (
        ("VERIFY_TABLES", set(VERIFY_TABLES)),
        ("STRICT_COMPLETE_TABLES", set(STRICT_COMPLETE_TABLES)),
        ("MIGRATION_ABORT_IF_ZERO", set(MIGRATION_ABORT_IF_ZERO)),
        ("SUBSCRIPTION_TABLES", set(SUBSCRIPTION_TABLES)),
    ):
        overlap = CLEANABLE_TABLES & critical
        assert not overlap, f"cleanup rules must not touch {name}: {sorted(overlap)}"
    print(f"OK: {len(CLEANABLE_TABLES)} cleanable tables are all non-critical")


def test_rule_ids_unique_and_tables_disjoint():
    ids = [r.id for r in CLEANUP_RULES]
    assert len(ids) == len(set(ids)), f"duplicate rule ids: {ids}"
    seen: set[str] = set()
    for rule in CLEANUP_RULES:
        clash = seen & set(rule.tables)
        assert not clash, f"table listed in two rules: {sorted(clash)}"
        seen.update(rule.tables)
    for rule in CLEANUP_RULES:
        for lang in ("en", "fa", "ru"):
            assert rule.label.get(lang), f"{rule.id} missing {lang} label"
            assert rule.description.get(lang), f"{rule.id} missing {lang} description"
    print("OK: rule ids unique, tables disjoint, labels complete")


def test_resolve_tables_ignores_unknown_ids():
    assert resolve_tables(["node_traffic_history"]) == {
        "node_user_usages",
        "node_usages",
        "node_usage_reset_logs",
    }
    assert resolve_tables(["does_not_exist"]) == set()
    assert resolve_tables(None) == set()
    assert resolve_tables([]) == set()
    print("OK: resolve_tables ignores unknown ids")


# ---------------------------------------------------------- statement scanner


def test_statement_completion_respects_quotes():
    assert _statement_is_complete("INSERT INTO t VALUES (1);")
    assert not _statement_is_complete("INSERT INTO t VALUES (1)")
    # ';' inside a literal does not end the statement
    assert not _statement_is_complete("INSERT INTO t VALUES ('a;b'")
    assert _statement_is_complete("INSERT INTO t VALUES ('a;b');")
    # escaped quote forms
    assert _statement_is_complete(r"INSERT INTO t VALUES ('it\'s');")
    assert _statement_is_complete("INSERT INTO t VALUES ('it''s');")
    # newline inside a literal
    assert not _statement_is_complete("INSERT INTO t VALUES ('line1\nline2'")
    assert _statement_is_complete("INSERT INTO t VALUES ('line1\nline2');")
    print("OK: statement scanner respects quoting")


def test_count_value_tuples():
    assert _count_value_tuples("INSERT INTO t VALUES (1),(2),(3);") == 3
    assert _count_value_tuples("INSERT INTO t VALUES (1);") == 1
    # parentheses inside literals must not inflate the count
    assert _count_value_tuples("INSERT INTO t VALUES ('a(b)c'),('d');") == 2
    # nested parens in a single row stay one row
    assert _count_value_tuples("INSERT INTO t VALUES (1,POINT(2,3));") == 1
    print("OK: value tuple counting")


# --------------------------------------------------------- pg COPY filtering


PG_DUMP = """--
-- PostgreSQL database dump
--
CREATE TABLE public.users (id integer NOT NULL, username text);
CREATE TABLE public.node_usages (id bigint NOT NULL, used_traffic bigint);

COPY public.users (id, username) FROM stdin;
1\talice
2\tbob
\\.

COPY public.node_usages (id, used_traffic) FROM stdin;
1\t1000
2\t2000
3\t3000
\\.

SELECT pg_catalog.setval('public.node_usages_id_seq', 3, true);
ALTER TABLE ONLY public.users ADD CONSTRAINT users_pkey PRIMARY KEY (id);
"""


def test_pg_copy_block_dropped_for_target_only():
    out, stats = _filter(PG_DUMP, {"node_usages"})

    assert stats.rows == {"node_usages": 3}
    assert stats.bytes["node_usages"] > 0

    # target payload gone, statement structure kept
    assert "1\t1000" not in out
    assert "COPY public.node_usages (id, used_traffic) FROM stdin;" in out
    # non-target data fully intact
    assert "1\talice" in out and "2\tbob" in out
    # schema and sequence untouched
    assert "CREATE TABLE public.node_usages" in out
    assert "setval('public.node_usages_id_seq'" in out
    assert "users_pkey" in out
    print("OK: pg COPY block dropped for target table only")


def test_pg_copy_no_rules_is_byte_identical():
    out, stats = _filter(PG_DUMP, set())
    assert out == PG_DUMP
    assert stats.total_rows == 0
    print("OK: empty rule set is byte-identical passthrough")


def test_pg_copy_terminator_not_confused_by_data():
    """A data value that looks like the terminator must not end the block early."""
    dump = (
        "COPY public.node_usages (id, note) FROM stdin;\n"
        "1\t\\\\.\n"          # value is an escaped backslash + dot, not a terminator
        "2\tplain\n"
        "\\.\n"
        "COPY public.users (id) FROM stdin;\n"
        "7\n"
        "\\.\n"
    )
    out, stats = _filter(dump, {"node_usages"})
    assert stats.rows == {"node_usages": 2}
    assert "7" in out
    assert "plain" not in out
    print("OK: COPY terminator not confused by escaped data")


def test_quoted_and_schemaless_copy_forms():
    for header in (
        'COPY "node_usages" (id) FROM stdin;',
        "COPY node_usages (id) FROM stdin;",
        'COPY public."node_usages" (id) FROM stdin;',
        "COPY public.node_usages FROM stdin;",
    ):
        dump = f"{header}\n1\n2\n\\.\n"
        _, stats = _filter(dump, {"node_usages"})
        assert stats.rows == {"node_usages": 2}, f"failed for header: {header}"
    print("OK: COPY header variants recognised")


# ------------------------------------------------------ mysql INSERT filtering


MYSQL_DUMP = """-- MySQL dump 10.13
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (`id` int NOT NULL, `username` varchar(64));
LOCK TABLES `users` WRITE;
INSERT INTO `users` VALUES (1,'alice'),(2,'bob');
UNLOCK TABLES;

DROP TABLE IF EXISTS `node_user_usages`;
CREATE TABLE `node_user_usages` (`id` bigint NOT NULL, `used_traffic` bigint);
LOCK TABLES `node_user_usages` WRITE;
/*!40000 ALTER TABLE `node_user_usages` DISABLE KEYS */;
INSERT INTO `node_user_usages` VALUES (1,100),(2,200),(3,300);
INSERT INTO `node_user_usages` VALUES (4,400);
/*!40000 ALTER TABLE `node_user_usages` ENABLE KEYS */;
UNLOCK TABLES;
"""


def test_mysql_inserts_dropped_for_target_only():
    out, stats = _filter(MYSQL_DUMP, {"node_user_usages"})

    assert stats.rows == {"node_user_usages": 4}
    assert "INSERT INTO `node_user_usages`" not in out
    # everything else preserved verbatim
    assert "INSERT INTO `users` VALUES (1,'alice'),(2,'bob');" in out
    assert "CREATE TABLE `node_user_usages`" in out
    assert "DISABLE KEYS" in out and "ENABLE KEYS" in out
    assert "LOCK TABLES `node_user_usages` WRITE;" in out
    print("OK: mysql INSERT statements dropped for target table only")


def test_multiline_insert_with_embedded_sql_text():
    """A newline + INSERT-looking text inside a literal must stay inside it."""
    dump = (
        "INSERT INTO `node_usages` VALUES (1,'a\n"
        "INSERT INTO `users` VALUES (99,\\'evil\\');\n"
        "still the same literal'),(2,'b');\n"
        "INSERT INTO `users` VALUES (5,'real');\n"
    )
    out, stats = _filter(dump, {"node_usages"})

    assert stats.rows == {"node_usages": 2}
    # the embedded text was part of the dropped literal, not a separate statement
    assert "evil" not in out
    # the genuine users insert survived
    assert "INSERT INTO `users` VALUES (5,'real');" in out
    print("OK: multi-line INSERT with embedded SQL text handled")


def test_unterminated_statement_is_emitted_not_swallowed():
    """A truncated dump must keep its trailing bytes rather than lose them."""
    dump = "INSERT INTO `node_usages` VALUES (1,'unfinished\n"
    out, stats = _filter(dump, {"node_usages"})
    assert out == dump
    assert stats.total_rows == 0
    print("OK: unterminated trailing statement preserved")


def test_similar_table_names_not_matched():
    dump = (
        "INSERT INTO `node_usages` VALUES (1);\n"
        "INSERT INTO `node_usages_archive` VALUES (2);\n"
        "INSERT INTO `my_node_usages` VALUES (3);\n"
    )
    out, stats = _filter(dump, {"node_usages"})
    assert stats.rows == {"node_usages": 1}
    assert "node_usages_archive" in out
    assert "my_node_usages" in out
    print("OK: prefix/suffix table names not matched")


def test_filter_is_idempotent():
    once, _ = _filter(MYSQL_DUMP, {"node_user_usages"})
    twice, stats2 = _filter(once, {"node_user_usages"})
    assert once == twice
    assert stats2.total_rows == 0
    print("OK: filtering is idempotent")


# ------------------------------------------------------------------- sqlite


def _make_sqlite(path: Path, usage_rows: int = 500) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("CREATE TABLE node_usages (id INTEGER PRIMARY KEY, used_traffic INTEGER)")
    conn.execute("CREATE TABLE node_user_usages (id INTEGER PRIMARY KEY, blob TEXT)")
    conn.executemany("INSERT INTO users (username) VALUES (?)", [(f"u{i}",) for i in range(20)])
    conn.executemany(
        "INSERT INTO node_usages (used_traffic) VALUES (?)", [(i,) for i in range(usage_rows)]
    )
    conn.executemany(
        "INSERT INTO node_user_usages (blob) VALUES (?)", [("x" * 200,) for _ in range(usage_rows)]
    )
    conn.commit()
    conn.close()


def test_sqlite_cleanup_removes_only_targets_and_shrinks(tmp_path):
    db = tmp_path / "db.sqlite3"
    _make_sqlite(db)
    before = db.stat().st_size

    measured = measure_sqlite(db, {"node_user_usages"})
    assert measured.rows == {"node_user_usages": 500}

    stats = clean_sqlite(db, {"node_user_usages"})
    assert stats.rows == {"node_user_usages": 500}
    assert db.stat().st_size < before, "VACUUM should reclaim space"

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 20
        assert conn.execute("SELECT COUNT(*) FROM node_usages").fetchone()[0] == 500
        assert conn.execute("SELECT COUNT(*) FROM node_user_usages").fetchone()[0] == 0
    finally:
        conn.close()
    print("OK: sqlite cleanup removes only targets and reclaims space")


def test_sqlite_missing_table_is_not_an_error(tmp_path):
    db = tmp_path / "db.sqlite3"
    _make_sqlite(db, usage_rows=5)
    stats = clean_sqlite(db, {"table_that_does_not_exist"})
    assert stats.total_rows == 0
    print("OK: missing sqlite table tolerated")


# ------------------------------------------------------------------ archives


def _make_zip(tmp_path: Path, name: str, files: dict[str, bytes | str]) -> Path:
    z = tmp_path / name
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, content in files.items():
            if isinstance(content, str):
                zf.writestr(arc, content)
            else:
                zf.writestr(arc, content)
    return z


def test_analyze_reports_per_rule_numbers(tmp_path):
    z = _make_zip(tmp_path, "backup.zip", {".env": ENV_TEXT, "db_backup.sql": MYSQL_DUMP})
    report = analyze_cleanup(z)

    assert report["available"] is True
    assert report["removable_rows"] == 4
    by_id = {r["id"]: r for r in report["rules"]}
    assert by_id["node_traffic_history"]["rows"] == 4
    assert by_id["usage_logs"]["rows"] == 0
    assert set(report["default_rule_ids"]) == set(default_rule_ids())
    print("OK: analyze reports per-rule numbers")


def test_apply_never_touches_input(tmp_path):
    z = _make_zip(tmp_path, "backup.zip", {".env": ENV_TEXT, "db_backup.sql": MYSQL_DUMP})
    original = z.read_bytes()
    out = tmp_path / "cleaned.zip"

    apply_cleanup(z, ["node_traffic_history"], out)

    assert z.read_bytes() == original, "input archive must be untouched"
    assert out.is_file()
    print("OK: apply leaves the input archive byte-identical")


def test_apply_measured_numbers_match_analyze(tmp_path):
    z = _make_zip(tmp_path, "backup.zip", {".env": ENV_TEXT, "db_backup.sql": MYSQL_DUMP})
    report = analyze_cleanup(z)
    result = apply_cleanup(z, ["node_traffic_history"], tmp_path / "cleaned.zip")

    predicted = {r["id"]: r["rows"] for r in report["rules"]}["node_traffic_history"]
    assert result["removed_rows"] == predicted, "preview must match what apply did"
    print("OK: preview numbers equal applied numbers")


def test_apply_preserves_all_archive_members(tmp_path):
    files = {
        ".env": ENV_TEXT,
        "db_backup.sql": MYSQL_DUMP,
        "xray_config.json": '{"log": {}}',
        "certs/fullchain.pem": "PEM",
        "templates/sub.html": "<html></html>",
    }
    z = _make_zip(tmp_path, "backup.zip", files)
    out = tmp_path / "cleaned.zip"
    apply_cleanup(z, ["node_traffic_history"], out)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert names == set(files), f"member set changed: {names ^ set(files)}"
        # non-dump members must be byte-identical
        for arc in ("xray_config.json", "certs/fullchain.pem", "templates/sub.html", ".env"):
            assert zf.read(arc).decode() == files[arc]
        cleaned_sql = zf.read("db_backup.sql").decode()
        assert "INSERT INTO `node_user_usages`" not in cleaned_sql
        assert "INSERT INTO `users` VALUES (1,'alice'),(2,'bob');" in cleaned_sql
    print("OK: apply preserves every archive member")


def test_apply_with_no_rules_is_content_preserving(tmp_path):
    files = {".env": ENV_TEXT, "db_backup.sql": MYSQL_DUMP}
    z = _make_zip(tmp_path, "backup.zip", files)
    out = tmp_path / "cleaned.zip"
    result = apply_cleanup(z, [], out)

    assert result["removed_rows"] == 0
    with zipfile.ZipFile(out) as zf:
        assert zf.read("db_backup.sql").decode() == MYSQL_DUMP
        assert zf.read(".env").decode() == ENV_TEXT
    print("OK: apply with no rules preserves content")


def test_apply_multi_layout_pg_dump(tmp_path):
    manifest = "pasarguard\tpg\t1\tpasarguard.sql\t2.17.2\n"
    z = _make_zip(
        tmp_path,
        "backup.zip",
        {
            ".env": ENV_TEXT,
            "pg_dump/manifest.tsv": manifest,
            "pg_dump/globals.sql": "CREATE ROLE pg;\n",
            "pg_dump/pasarguard.sql": PG_DUMP,
        },
    )
    out = tmp_path / "cleaned.zip"
    result = apply_cleanup(z, ["node_traffic_history"], out)

    assert result["removed_rows"] == 3
    with zipfile.ZipFile(out) as zf:
        # manifest must stay exactly as-is: filenames are unchanged
        assert zf.read("pg_dump/manifest.tsv").decode() == manifest
        assert zf.read("pg_dump/globals.sql").decode() == "CREATE ROLE pg;\n"
        sql = zf.read("pg_dump/pasarguard.sql").decode()
        assert "1\talice" in sql
        assert "1\t1000" not in sql
    print("OK: multi-layout pg_dump cleaned, manifest untouched")


def test_apply_sqlite_layout(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_sqlite(src / "db.sqlite3")
    (src / ".env").write_text("SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:///db/db.sqlite3\n")
    z = tmp_path / "backup.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src / ".env", ".env")
        zf.write(src / "db.sqlite3", "db.sqlite3")

    out = tmp_path / "cleaned.zip"
    result = apply_cleanup(z, ["node_traffic_history"], out)

    assert result["removed_rows"] == 1000  # node_usages + node_user_usages
    assert result["size_after"] < result["size_before"]

    extracted = tmp_path / "check"
    with zipfile.ZipFile(out) as zf:
        zf.extractall(extracted)
    conn = sqlite3.connect(str(extracted / "db.sqlite3"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 20
        assert conn.execute("SELECT COUNT(*) FROM node_usages").fetchone()[0] == 0
    finally:
        conn.close()
    print("OK: sqlite-layout archive cleaned")


def test_apply_is_idempotent_across_archives(tmp_path):
    z = _make_zip(tmp_path, "backup.zip", {".env": ENV_TEXT, "db_backup.sql": MYSQL_DUMP})
    first = tmp_path / "c1.zip"
    second = tmp_path / "c2.zip"
    r1 = apply_cleanup(z, ["node_traffic_history"], first)
    r2 = apply_cleanup(first, ["node_traffic_history"], second)

    assert r1["removed_rows"] == 4
    assert r2["removed_rows"] == 0
    with zipfile.ZipFile(first) as a, zipfile.ZipFile(second) as b:
        assert a.read("db_backup.sql") == b.read("db_backup.sql")
    print("OK: cleaning an already-cleaned archive is a no-op")


def test_nested_backup_root_is_found(tmp_path):
    z = _make_zip(
        tmp_path,
        "backup.zip",
        {"pasarguard-backup-2026/.env": ENV_TEXT, "pasarguard-backup-2026/db_backup.sql": MYSQL_DUMP},
    )
    report = analyze_cleanup(z)
    assert report["removable_rows"] == 4

    out = tmp_path / "cleaned.zip"
    apply_cleanup(z, ["node_traffic_history"], out)
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {
            "pasarguard-backup-2026/.env",
            "pasarguard-backup-2026/db_backup.sql",
        }
        assert "INSERT INTO `node_user_usages`" not in zf.read(
            "pasarguard-backup-2026/db_backup.sql"
        ).decode()
    print("OK: nested backup root handled, paths preserved")


def test_non_utf8_bytes_round_trip(tmp_path):
    """A latin-1 mysqldump must come back byte-identical outside dropped rows.

    A lossy decode here would silently delete bytes from user data, which is
    exactly the corruption this feature must never cause.
    """
    raw = (
        b"CREATE TABLE `users` (`id` int, `name` varchar(64));\n"
        b"INSERT INTO `users` VALUES (1,'caf\xe9'),(2,'na\xefve');\n"
        b"INSERT INTO `node_usages` VALUES (1,100);\n"
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "db_backup.sql").write_bytes(raw)
    (src / ".env").write_text(ENV_TEXT)
    z = tmp_path / "backup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(src / ".env", ".env")
        zf.write(src / "db_backup.sql", "db_backup.sql")

    out = tmp_path / "cleaned.zip"
    result = apply_cleanup(z, ["node_traffic_history"], out)
    assert result["removed_rows"] == 1

    with zipfile.ZipFile(out) as zf:
        cleaned = zf.read("db_backup.sql")
    assert b"caf\xe9" in cleaned, "non-UTF8 bytes must survive untouched"
    assert b"na\xefve" in cleaned
    assert b"node_usages" not in cleaned.split(b"\n")[-2] or b"INSERT INTO `node_usages`" not in cleaned
    # the only removed content is the targeted INSERT line
    expected = raw.replace(b"INSERT INTO `node_usages` VALUES (1,100);\n", b"")
    assert cleaned == expected
    print("OK: non-UTF8 dump bytes round-trip losslessly")


def test_crlf_line_endings_preserved(tmp_path):
    raw = (
        "CREATE TABLE `users` (`id` int);\r\n"
        "INSERT INTO `users` VALUES (1);\r\n"
        "INSERT INTO `node_usages` VALUES (1,100);\r\n"
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "db_backup.sql").write_bytes(raw.encode())
    (src / ".env").write_text(ENV_TEXT)
    z = tmp_path / "backup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(src / ".env", ".env")
        zf.write(src / "db_backup.sql", "db_backup.sql")

    out = tmp_path / "cleaned.zip"
    apply_cleanup(z, ["node_traffic_history"], out)
    with zipfile.ZipFile(out) as zf:
        cleaned = zf.read("db_backup.sql").decode()
    assert "INSERT INTO `users` VALUES (1);\r\n" in cleaned
    assert "node_usages" not in cleaned
    assert "\r\n" in cleaned and "\n\n" not in cleaned
    print("OK: CRLF line endings preserved")


def test_unicode_payload_preserved():
    dump = (
        "INSERT INTO `users` VALUES (1,'کاربر_تست'),(2,'Пользователь');\n"
        "INSERT INTO `node_usages` VALUES (1,'مصرف');\n"
    )
    out, stats = _filter(dump, {"node_usages"})
    assert "کاربر_تست" in out and "Пользователь" in out
    assert "مصرف" not in out
    assert stats.rows == {"node_usages": 1}
    print("OK: unicode payloads preserved")


def test_empty_copy_block_is_untouched():
    dump = "COPY public.node_usages (id) FROM stdin;\n\\.\nCREATE INDEX ix ON public.users (id);\n"
    out, stats = _filter(dump, {"node_usages"})
    assert out == dump
    assert stats.total_rows == 0
    print("OK: empty COPY block untouched")


def test_large_dump_is_streamed_not_buffered(tmp_path):
    """Filtering must not scale with file size in memory."""
    import tracemalloc

    big = tmp_path / "db_backup.sql"
    with open(big, "w", encoding="utf-8") as fh:
        fh.write("COPY public.node_usages (id, payload) FROM stdin;\n")
        for i in range(200_000):
            fh.write(f"{i}\t{'x' * 200}\n")
        fh.write("\\.\n")
        fh.write("COPY public.users (id) FROM stdin;\n1\n\\.\n")

    size = big.stat().st_size
    assert size > 30 * 1024 * 1024, f"fixture too small to be meaningful: {size}"

    tracemalloc.start()
    with open(big, "r", encoding="utf-8", errors="surrogateescape", newline="") as src:
        stats = filter_sql_stream(src, None, {"node_usages"})
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert stats.rows == {"node_usages": 200_000}
    assert peak < size / 10, f"peak memory {peak} vs file size {size} — not streaming"
    print(f"OK: {size // (1024 * 1024)}MiB dump filtered with {peak // 1024}KiB peak memory")


def test_rejects_unsafe_zip_entries(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    try:
        analyze_cleanup(z)
    except ValueError as e:
        assert "Unsafe zip entry" in str(e)
        print("OK: unsafe zip entries rejected")
        return
    raise AssertionError("expected ValueError for path traversal entry")


# -------------------------------------------------- verification and fallback


def _pg_backup_zip(tmp_path: Path, name: str = "backup.zip") -> Path:
    return _make_zip(tmp_path, name, {".env": ENV_TEXT, "db_backup.sql": MYSQL_DUMP})


def test_verify_accepts_a_correct_cleanup(tmp_path):
    from app.services.backup_cleanup import verify_cleaned_archive

    src = _pg_backup_zip(tmp_path)
    out = tmp_path / "cleaned.zip"
    apply_cleanup(src, ["node_traffic_history"], out)

    ok, reason = verify_cleaned_archive(src, out)
    assert ok, reason
    print("OK: verification accepts a correct cleanup")


def test_verify_rejects_archive_that_lost_critical_rows(tmp_path):
    """Simulate a filter bug that eats users rows — verification must catch it."""
    from app.services.backup_cleanup import verify_cleaned_archive

    src = _pg_backup_zip(tmp_path)
    broken = tmp_path / "broken.zip"
    damaged_sql = MYSQL_DUMP.replace("INSERT INTO `users` VALUES (1,'alice'),(2,'bob');\n", "")
    with zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".env", ENV_TEXT)
        zf.writestr("db_backup.sql", damaged_sql)

    ok, reason = verify_cleaned_archive(src, broken)
    assert not ok
    assert "table_counts" in reason
    print(f"OK: verification rejects lost critical rows ({reason})")


def test_verify_rejects_archive_that_lost_env(tmp_path):
    from app.services.backup_cleanup import verify_cleaned_archive

    src = _pg_backup_zip(tmp_path)
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("db_backup.sql", MYSQL_DUMP)

    ok, reason = verify_cleaned_archive(src, broken)
    assert not ok
    print(f"OK: verification rejects a dropped .env ({reason})")


def _stage_upload(tmp_path, monkeypatch, zip_path: Path) -> str:
    """Put a zip where get_upload_path/UPLOAD_DIR will find it."""
    import app.config as config
    import app.services.upload as upload_mod

    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "UPLOAD_DIR", uploads, raising=False)
    monkeypatch.setattr(upload_mod, "UPLOAD_DIR", uploads, raising=False)

    upload_id = "src000000001"
    d = uploads / upload_id
    d.mkdir(parents=True, exist_ok=True)
    (d / zip_path.name).write_bytes(zip_path.read_bytes())
    return upload_id


def test_clean_upload_returns_new_id_and_keeps_original(tmp_path, monkeypatch):
    from app.services.backup_cleanup import clean_upload

    src_zip = _pg_backup_zip(tmp_path)
    upload_id = _stage_upload(tmp_path, monkeypatch, src_zip)
    original_bytes = (tmp_path / "uploads" / upload_id / src_zip.name).read_bytes()

    result = clean_upload(upload_id, ["node_traffic_history"])

    assert result["applied"] is True, result
    assert result["upload_id"] != upload_id
    assert result["source_upload_id"] == upload_id
    assert result["removed_rows"] == 4

    # original upload untouched and still restorable
    assert (tmp_path / "uploads" / upload_id / src_zip.name).read_bytes() == original_bytes

    # the new id resolves like any other upload
    from app.services.upload import get_upload_path

    new_path = get_upload_path(result["upload_id"])
    assert new_path and Path(new_path).is_file() and new_path.endswith(".zip")
    print("OK: clean_upload yields a usable new upload id, original intact")


def test_clean_upload_declines_instead_of_raising(tmp_path, monkeypatch):
    from app.services.backup_cleanup import clean_upload

    src_zip = _pg_backup_zip(tmp_path)
    upload_id = _stage_upload(tmp_path, monkeypatch, src_zip)

    # unknown upload
    missing = clean_upload("nope", ["node_traffic_history"])
    assert missing["applied"] is False and missing["upload_id"] == "nope"

    # no rules selected
    none_selected = clean_upload(upload_id, [])
    assert none_selected["applied"] is False
    assert none_selected["upload_id"] == upload_id

    # unknown rule ids resolve to no tables
    unknown = clean_upload(upload_id, ["not_a_rule"])
    assert unknown["applied"] is False
    assert unknown["upload_id"] == upload_id
    print("OK: clean_upload declines rather than raising")


def test_clean_upload_falls_back_when_verification_fails(tmp_path, monkeypatch):
    """A cleanup that damages the archive must never be handed to the restore."""
    import app.services.backup_cleanup as bc

    src_zip = _pg_backup_zip(tmp_path)
    upload_id = _stage_upload(tmp_path, monkeypatch, src_zip)

    monkeypatch.setattr(
        bc, "verify_cleaned_archive", lambda a, b: (False, "simulated corruption")
    )
    result = bc.clean_upload(upload_id, ["node_traffic_history"])

    assert result["applied"] is False
    assert result["upload_id"] == upload_id, "must fall back to the original upload"
    assert "simulated corruption" in result["reason"]

    uploads = tmp_path / "uploads"
    assert [p.name for p in uploads.iterdir()] == [upload_id], "rejected output must be deleted"
    print("OK: failed verification falls back to the original upload")


def test_clean_upload_falls_back_when_apply_raises(tmp_path, monkeypatch):
    import app.services.backup_cleanup as bc

    src_zip = _pg_backup_zip(tmp_path)
    upload_id = _stage_upload(tmp_path, monkeypatch, src_zip)

    def boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(bc, "apply_cleanup", boom)
    result = bc.clean_upload(upload_id, ["node_traffic_history"])

    assert result["applied"] is False
    assert result["upload_id"] == upload_id
    assert "disk exploded" in result["reason"]
    uploads = tmp_path / "uploads"
    assert [p.name for p in uploads.iterdir()] == [upload_id]
    print("OK: an exception during apply falls back to the original upload")


def test_clean_upload_declines_when_disk_is_tight(tmp_path, monkeypatch):
    import app.services.backup_cleanup as bc

    src_zip = _pg_backup_zip(tmp_path)
    upload_id = _stage_upload(tmp_path, monkeypatch, src_zip)

    monkeypatch.setattr(bc, "_has_room_for_cleanup", lambda z, w: False)
    result = bc.clean_upload(upload_id, ["node_traffic_history"])

    assert result["applied"] is False
    assert result["upload_id"] == upload_id
    assert "disk" in result["reason"]
    print("OK: cleanup declines when free disk space is tight")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
