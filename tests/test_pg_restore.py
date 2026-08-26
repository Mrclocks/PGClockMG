"""Unit tests for smart PasarGuard restore helpers (all DB families)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.pg_restore import (
    soft_db_family,
    filter_timescaledb_extension_sql,
    filter_globals_sql,
    parse_timescale_wanted,
    detect_ts_mismatch_from_text,
    is_auth_failure_text,
    is_ts_catalog_mismatch_error,
    detect_dump_chunk_catalog_era,
    detect_dump_ts_catalog_floor,
    detect_backup_ts_catalog_floor,
    ts_floor_from_error_text,
    ts_pin_for_floor,
    resolve_wanted_ts_for_live,
    wanted_ts_for_restore_retry,
    collect_backup_ts_versions,
    TS_LAST_SCHEMA_NAME_CHUNK,
    TS_FIRST_RELID_CHUNK,
    _sql_literal,
    _set_env_var,
    _parse_manifest_ts_versions,
    analyze_pasarguard_backup,
    explain_restore_error,
    discover_backup_artifacts,
)


def test_soft_db_family_matrix():
    assert soft_db_family("mysql", "mariadb")
    assert soft_db_family("mariadb", "mysql")
    # Plain PG → Timescale is soft; Timescale → plain PG needs convert
    assert soft_db_family("postgresql", "timescaledb")
    assert not soft_db_family("timescaledb", "postgresql")
    assert soft_db_family("sqlite", "sqlite")
    assert soft_db_family("timescaledb", "timescaledb")
    assert soft_db_family("postgresql", "postgresql")
    assert not soft_db_family("sqlite", "mysql")
    assert not soft_db_family("mysql", "timescaledb")
    assert not soft_db_family("postgresql", "mysql")
    assert not soft_db_family(None, "mysql")
    print("OK: soft_db_family matrix")


def test_ts_to_ts_syncs_alembic_before_panel():
    from app.services.pg_restore import should_sync_alembic_before_panel_boot

    # timescaledb → timescaledb (same engine): needs_convert is False
    backup_db, final_db = "timescaledb", "timescaledb"
    needs_convert = bool(
        backup_db and final_db and backup_db != final_db and not soft_db_family(backup_db, final_db)
    )
    assert needs_convert is False
    assert should_sync_alembic_before_panel_boot(needs_convert) is True

    # convert path skips pre-panel sync
    assert should_sync_alembic_before_panel_boot(True) is False
    print("OK: ts→ts syncs alembic before panel boot")


def test_ensure_timescaledb_forces_post_restore_when_on():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.services import pg_restore as mod

    job = MagicMock()
    job.log = MagicMock()
    calls: list[str] = []

    async def fake_run(_job, cmd, **_kwargs):
        joined = " ".join(str(c) for c in cmd)
        calls.append(joined)
        if "current_setting" in joined:
            return True, "on\n"
        if "timescaledb_post_restore" in joined:
            return True, "t\n"
        return True, ""

    async def _go():
        with (
            patch.object(mod, "_detect_db_container", AsyncMock(return_value="timescaledb")),
            patch.object(mod, "_run", side_effect=fake_run),
        ):
            await mod._ensure_timescaledb_not_in_restore_mode(
                job, "secret", "pasarguard", "pasarguard",
            )

    asyncio.run(_go())
    assert any("timescaledb_post_restore" in c for c in calls)
    log_text = " ".join(str(c.args[0]) for c in job.log.call_args_list if c.args)
    assert "restore mode" in log_text.lower()
    print("OK: ensure post_restore when restoring=on")


def test_ensure_timescaledb_forces_post_restore_when_check_fails():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.services import pg_restore as mod

    job = MagicMock()
    job.log = MagicMock()
    posts = {"n": 0}

    async def fake_run(_job, cmd, **_kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "current_setting" in joined:
            return False, "Timeout"
        if "timescaledb_post_restore" in joined:
            posts["n"] += 1
            return True, "t\n"
        return True, ""

    async def _go():
        with (
            patch.object(mod, "_detect_db_container", AsyncMock(return_value="timescaledb")),
            patch.object(mod, "_run", side_effect=fake_run),
        ):
            await mod._ensure_timescaledb_not_in_restore_mode(
                job, "secret", "pasarguard", "pasarguard",
            )

    asyncio.run(_go())
    assert posts["n"] >= 1, "must force post_restore when GUC check fails"
    log_text = " ".join(str(c.args[0]) for c in job.log.call_args_list if c.args)
    assert "inconclusive" in log_text.lower()
    print("OK: ensure post_restore when check inconclusive")


def test_filter_timescaledb_extension_sql():
    sql = "\n".join([
        "CREATE TABLE t(id int);",
        "CREATE EXTENSION timescaledb CASCADE;",
        "CREATE EXTENSION IF NOT EXISTS timescaledb;",
        "DROP EXTENSION IF EXISTS timescaledb;",
        "INSERT INTO t VALUES (1);",
    ])
    out = filter_timescaledb_extension_sql(sql)
    assert "CREATE TABLE" in out
    assert "INSERT INTO" in out
    assert "timescaledb" not in out.lower()
    print("OK: filter timescaledb extension sql")


def test_filter_timescaledb_strip_all_for_plain_pg():
    sql = "\n".join([
        "CREATE TABLE users (id int);",
        "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;",
        "SELECT create_hypertable('metrics', 'time');",
        "SELECT timescaledb_pre_restore();",
        "COMMENT ON EXTENSION timescaledb IS 'x';",
        "INSERT INTO users VALUES (1);",
    ])
    out = filter_timescaledb_extension_sql(sql, strip_all=True)
    assert "CREATE TABLE users" in out
    assert "INSERT INTO users" in out
    assert "timescaledb" not in out.lower()
    assert "create_hypertable" not in out.lower()
    print("OK: strip_all timescaledb for plain PostgreSQL")


def test_parse_timescale_wanted():
    assert parse_timescale_wanted(["2.28.1", "2.28.1"]) == "2.28.1"
    assert parse_timescale_wanted(["latest", "2.17.2"]) == "2.17.2"
    assert parse_timescale_wanted([]) is None
    print("OK: parse_timescale_wanted")


def test_detect_ts_mismatch_from_official_error():
    text = """
ERROR: TimescaleDB version mismatch for database "pasarguard"
  Backup version: 2.28.1
  Target server version: 2.28.2
The restore was stopped BEFORE changing anything
"""
    pair = detect_ts_mismatch_from_text(text)
    assert pair == ("2.28.1", "2.28.2")
    print("OK: detect timescale mismatch text")


def test_is_ts_catalog_mismatch_error_schema_name():
    err = 'ERROR:  column "schema_name" of relation "chunk" does not exist'
    assert is_ts_catalog_mismatch_error(err)
    assert is_ts_catalog_mismatch_error(
        'ERROR: column "relid" of relation "chunk" does not exist'
    )
    assert not is_ts_catalog_mismatch_error('ERROR: relation "users" does not exist')
    print("OK: catalog mismatch detection")


def test_detect_dump_chunk_catalog_era():
    old = (
        "COPY _timescaledb_catalog.chunk (id, hypertable_id, schema_name, "
        "table_name, compressed_chunk_id, status) FROM stdin;\n"
    )
    new = (
        "COPY _timescaledb_catalog.chunk (id, hypertable_id, relid, status) "
        "FROM stdin;\n"
    )
    assert detect_dump_chunk_catalog_era(old) == "schema_name"
    assert detect_dump_chunk_catalog_era(new) == "relid"
    assert detect_dump_chunk_catalog_era("COPY public.users (id) FROM stdin;") is None
    print("OK: dump chunk catalog era")


def test_resolve_wanted_ts_for_live_pins_pre_229():
    # Live latest (2.29+) + old dump fingerprint → pin 2.28.3
    assert (
        resolve_wanted_ts_for_live(None, live_ver="2.29.1", catalog_era="schema_name")
        == TS_LAST_SCHEMA_NAME_CHUNK
    )
    # Live already 2.28.x + old dump → no realign
    assert (
        resolve_wanted_ts_for_live(None, live_ver="2.28.1", catalog_era="schema_name")
        is None
    )
    # Explicit older version still preferred when live is 2.29+
    assert (
        resolve_wanted_ts_for_live("2.17.2", live_ver="2.29.0", catalog_era="schema_name")
        == "2.17.2"
    )
    # New dump on old live → pin 2.29.0
    assert (
        resolve_wanted_ts_for_live(None, live_ver="2.28.3", catalog_era="relid")
        == TS_FIRST_RELID_CHUNK
    )
    print("OK: resolve wanted ts for live")


def test_wanted_ts_for_restore_retry_from_catalog_error():
    err = 'ERROR:  column "schema_name" of relation "chunk" does not exist'
    assert (
        wanted_ts_for_restore_retry(err, {"timescaledb_versions": [], "timescaledb_chunk_catalog": "schema_name"})
        == TS_LAST_SCHEMA_NAME_CHUNK
    )
    assert (
        wanted_ts_for_restore_retry(err, {})
        == TS_LAST_SCHEMA_NAME_CHUNK
    )
    print("OK: wanted ts for restore retry")


CONTINUOUS_AGG_228_COPY = (
    "COPY _timescaledb_catalog.continuous_agg (mat_hypertable_id, raw_hypertable_id, "
    "parent_mat_hypertable_id, user_view_schema, user_view_name, partial_view_schema, "
    "partial_view_name, direct_view_schema, direct_view_name, materialized_only, "
    "schema_change_timestamp) FROM stdin;\n"
)
CONTINUOUS_AGG_227_COPY = (
    "COPY _timescaledb_catalog.continuous_agg (mat_hypertable_id, raw_hypertable_id, "
    "parent_mat_hypertable_id, user_view_schema, user_view_name, partial_view_schema, "
    "partial_view_name, direct_view_schema, direct_view_name, materialized_only) "
    "FROM stdin;\n"
)
CONTINUOUS_AGG_228_ERROR = (
    'ERROR:  column "schema_change_timestamp" of relation "continuous_agg" does not exist'
)


def test_detect_dump_ts_catalog_floor():
    # continuous_agg.schema_change_timestamp arrived in TimescaleDB 2.28.0
    assert detect_dump_ts_catalog_floor(CONTINUOUS_AGG_228_COPY) == "2.28.0"
    assert detect_dump_ts_catalog_floor(CONTINUOUS_AGG_227_COPY) is None
    assert (
        detect_dump_ts_catalog_floor(
            "COPY _timescaledb_catalog.chunk (id, hypertable_id, relid) FROM stdin;\n"
        )
        == TS_FIRST_RELID_CHUNK
    )
    # Highest floor wins when a dump carries several markers
    assert (
        detect_dump_ts_catalog_floor(
            CONTINUOUS_AGG_228_COPY
            + "COPY _timescaledb_catalog.chunk (id, hypertable_id, relid) FROM stdin;\n"
        )
        == TS_FIRST_RELID_CHUNK
    )
    # Application tables never imply a Timescale floor
    assert detect_dump_ts_catalog_floor("COPY public.users (id, relid) FROM stdin;") is None
    assert detect_dump_ts_catalog_floor("") is None
    print("OK: dump timescale catalog floor")


def test_detect_backup_ts_catalog_floor_multi_layout():
    import tempfile
    import shutil

    td = Path(tempfile.mkdtemp(prefix="pg-ts-floor-"))
    try:
        pg = td / "pg_dump"
        pg.mkdir()
        (pg / "manifest.tsv").write_text("pasarguard\tpasarguard\t1\tpasarguard.sql\t\n", encoding="utf-8")
        (pg / "globals.sql").write_text("CREATE ROLE pasarguard;\n", encoding="utf-8")
        (pg / "pasarguard.sql").write_text(
            "--\n-- PostgreSQL database dump\n--\n"
            "COPY public.users (id, username) FROM stdin;\n1\tbob\n\\.\n"
            + CONTINUOUS_AGG_228_COPY
            + "1\t2\t\\N\tpublic\tv\t_timescaledb_internal\tp\t_timescaledb_internal\td\tt\t\\N\n\\.\n",
            encoding="utf-8",
        )
        assert detect_backup_ts_catalog_floor(td) == "2.28.0"

        (pg / "pasarguard.sql").write_text(CONTINUOUS_AGG_227_COPY, encoding="utf-8")
        assert detect_backup_ts_catalog_floor(td) is None
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("OK: backup timescale catalog floor (pg_dump layout)")


def test_detect_dump_ts_catalog_floor_new_tables():
    # A catalog table the older extension does not have at all
    assert (
        detect_dump_ts_catalog_floor(
            "COPY _timescaledb_catalog.chunk_column_stats (id, hypertable_id) FROM stdin;\n"
        )
        == "2.16.0"
    )
    # Generic names must never be read off an application table
    assert detect_dump_ts_catalog_floor("COPY public.bgw_job (id, name) FROM stdin;") is None
    assert (
        detect_dump_ts_catalog_floor("COPY _timescaledb_catalog.bgw_job (id) FROM stdin;")
        == "2.25.0"
    )
    print("OK: dump floor from catalog tables")


def test_ts_floor_from_error_text():
    assert ts_floor_from_error_text(CONTINUOUS_AGG_228_ERROR) == "2.28.0"
    assert (
        ts_floor_from_error_text(
            'ERROR:  relation "_timescaledb_catalog.chunk_column_stats" does not exist'
        )
        == "2.16.0"
    )
    assert (
        ts_floor_from_error_text('ERROR: column "relid" of relation "chunk" does not exist')
        == TS_FIRST_RELID_CHUNK
    )
    # Old dump on a new server is a ceiling, not a floor — no pin implied here
    assert ts_floor_from_error_text(
        'ERROR: column "schema_name" of relation "chunk" does not exist'
    ) is None
    assert ts_floor_from_error_text('ERROR: column "note" of relation "users" does not exist') is None
    print("OK: timescale floor from psql error text")


def test_is_ts_catalog_mismatch_error_continuous_agg():
    assert is_ts_catalog_mismatch_error(CONTINUOUS_AGG_228_ERROR)
    assert is_ts_catalog_mismatch_error(
        'ERROR: relation "_timescaledb_catalog.hypertable_cagg_settings" does not exist'
    )
    # Application-schema errors must stay out of the Timescale realign path
    assert not is_ts_catalog_mismatch_error(
        'ERROR: column "note" of relation "users" does not exist'
    )
    assert not is_ts_catalog_mismatch_error('ERROR: relation "hosts" does not exist')
    print("OK: catalog mismatch detection for continuous_agg")


def test_resolve_wanted_ts_for_live_honours_dump_floor():
    # The reported failure: 2.28.x backup, live 2.26.4, no explicit version in the archive
    assert (
        resolve_wanted_ts_for_live(
            None, live_ver="2.26.4", catalog_era="schema_name", min_ver="2.28.0"
        )
        == TS_LAST_SCHEMA_NAME_CHUNK
    )
    # Server already new enough → no realign, exactly as before the floor existed
    assert (
        resolve_wanted_ts_for_live(
            None, live_ver="2.28.1", catalog_era="schema_name", min_ver="2.28.0"
        )
        is None
    )
    # A stale/bogus explicit pin below the floor must not win
    assert (
        resolve_wanted_ts_for_live(
            "2.17.2", live_ver="2.26.4", catalog_era="schema_name", min_ver="2.28.0"
        )
        == TS_LAST_SCHEMA_NAME_CHUNK
    )
    # Explicit pin that satisfies the floor is still preferred
    assert (
        resolve_wanted_ts_for_live(
            "2.28.1", live_ver="2.26.4", catalog_era="schema_name", min_ver="2.28.0"
        )
        == "2.28.1"
    )
    # 2.29+ dumps keep landing on 2.29.0
    assert (
        resolve_wanted_ts_for_live(
            None, live_ver="2.26.4", catalog_era="relid", min_ver=TS_FIRST_RELID_CHUNK
        )
        == TS_FIRST_RELID_CHUNK
    )
    # Floor-free calls behave exactly like before
    assert (
        resolve_wanted_ts_for_live(None, live_ver="2.26.4", catalog_era="schema_name")
        is None
    )
    print("OK: resolve wanted ts honours dump floor")


def test_ts_pin_for_floor():
    assert ts_pin_for_floor("2.28.0") == TS_LAST_SCHEMA_NAME_CHUNK
    assert ts_pin_for_floor("2.28.0", "relid") == "2.28.0"
    assert ts_pin_for_floor(TS_FIRST_RELID_CHUNK) == TS_FIRST_RELID_CHUNK
    print("OK: ts pin for floor")


def test_wanted_ts_for_restore_retry_newer_backup():
    assert (
        wanted_ts_for_restore_retry(
            CONTINUOUS_AGG_228_ERROR,
            {"timescaledb_versions": [], "timescaledb_chunk_catalog": "schema_name"},
        )
        == TS_LAST_SCHEMA_NAME_CHUNK
    )
    assert wanted_ts_for_restore_retry(CONTINUOUS_AGG_228_ERROR, {}) == TS_LAST_SCHEMA_NAME_CHUNK
    # Floor from the analysis is enough when the error text is vaguer
    assert (
        wanted_ts_for_restore_retry(
            'ERROR: column "x" of relation "continuous_agg" does not exist',
            {"timescaledb_min_version": "2.28.0"},
        )
        == TS_LAST_SCHEMA_NAME_CHUNK
    )
    # Unrelated failures still trigger no image realign
    assert wanted_ts_for_restore_retry("ERROR: permission denied for schema public", {}) is None
    print("OK: wanted ts for restore retry (newer backup)")


def test_align_image_failed_pull_keeps_data_and_tag():
    """A tag that cannot be pulled must not stop containers or wipe the volume."""
    import asyncio
    import tempfile
    import shutil
    from unittest.mock import MagicMock, patch
    from app.services import pg_restore as mod

    td = Path(tempfile.mkdtemp(prefix="pg-align-pull-"))
    compose = td / "docker-compose.yml"
    original = "services:\n  timescaledb:\n    image: timescale/timescaledb:2.26.4-pg17\n"
    compose.write_text(original, encoding="utf-8")
    calls: list[tuple] = []

    async def fake_compose(_job, *args, **_kwargs):
        calls.append(args)
        return (False, "manifest unknown") if args[0] == "pull" else (True, "")

    job = MagicMock()
    job.log = MagicMock()

    async def _go():
        with (
            patch.object(mod, "PASARGUARD_DIR", td),
            patch.object(mod, "_compose", side_effect=fake_compose),
        ):
            await mod._align_timescaledb_image(job, TS_LAST_SCHEMA_NAME_CHUNK, wipe_data=True)

    try:
        raised = False
        try:
            asyncio.run(_go())
        except RuntimeError as e:
            raised = "could not be pulled" in str(e)
        assert raised, "failed pull must abort the restore"
        assert compose.read_text(encoding="utf-8") == original
        assert [c for c in calls if c[0] == "stop"] == []
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("OK: failed image pull keeps data and compose tag")


def test_explain_newer_ts_backup_error():
    exc = RuntimeError(f"Failed restoring pasarguard:\n{CONTINUOUS_AGG_228_ERROR}")
    info = explain_restore_error(exc, "timescaledb", "timescaledb")
    assert "2.28.0" in (info.get("en") or "")
    assert "2.28.0" in (info.get("fa") or "")
    assert any(TS_LAST_SCHEMA_NAME_CHUNK in c for c in info.get("causes_fa") or [])
    print("OK: explain newer timescale backup error")


def test_explain_schema_name_chunk_error():
    exc = RuntimeError(
        'PostgreSQL dump restore failed:\nERROR:  column "schema_name" of relation "chunk" does not exist'
    )
    info = explain_restore_error(exc, "timescaledb", "timescaledb")
    assert "schema_name" in (info.get("en") or "").lower() or "catalog" in (info.get("en") or "").lower()
    assert info.get("causes_fa")
    print("OK: explain schema_name chunk error")


def test_collect_backup_ts_from_compose_and_catalog():
    import tempfile
    import shutil

    td = Path(tempfile.mkdtemp(prefix="pg-ts-collect-"))
    try:
        (td / "db_backup.sql").write_text(
            "COPY _timescaledb_catalog.chunk (id, hypertable_id, schema_name, table_name) FROM stdin;\n1\t1\t_timescaledb_internal\t_hyper_1_1_chunk\n\\.\n",
            encoding="utf-8",
        )
        (td / "docker-compose.yml").write_text(
            "services:\n  timescaledb:\n    image: timescale/timescaledb:2.17.2-pg17\n",
            encoding="utf-8",
        )
        versions = collect_backup_ts_versions(td)
        assert "2.17.2" in versions
        from app.services.pg_restore import detect_backup_chunk_catalog_era
        assert detect_backup_chunk_catalog_era(td) == "schema_name"
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("OK: collect backup ts from compose + catalog")


def test_is_auth_failure_text():
    assert is_auth_failure_text("asyncpg.exceptions.ProtocolViolationError: SASL authentication failed")
    assert is_auth_failure_text("password authentication failed for user pasarguard")
    assert is_auth_failure_text("Access denied for user 'root'@'%'")
    assert not is_auth_failure_text("Application startup complete")
    print("OK: auth failure detection")


def test_sql_literal_escapes_quotes():
    assert _sql_literal("a'b") == "'a''b'"
    print("OK: sql literal")


def test_merge_env_preserves_password():
    backup = 'DB_PASSWORD="old"\nSQLALCHEMY_DATABASE_URL="x"\n'
    text = backup
    text = _set_env_var(text, "DB_PASSWORD", "live-secret")
    text = _set_env_var(text, "POSTGRES_PASSWORD", "live-secret")
    assert 'DB_PASSWORD="live-secret"' in text
    assert 'POSTGRES_PASSWORD="live-secret"' in text
    print("OK: env password preserve")


def test_parse_manifest_ts_versions():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pg = root / "pg_dump"
        pg.mkdir()
        (pg / "manifest.tsv").write_text(
            "pasarguard\tpasarguard\t1\tpasarguard.sql\t2.28.1\n",
            encoding="utf-8",
        )
        assert _parse_manifest_ts_versions(root) == ["2.28.1"]
    print("OK: manifest timescale versions")


def _make_backup_zip(dest: Path, db_url: str, layout: str = "single") -> Path:
    work = dest / "content"
    work.mkdir(parents=True, exist_ok=True)
    (work / ".env").write_text(f'SQLALCHEMY_DATABASE_URL="{db_url}"\nDB_PASSWORD="x"\n', encoding="utf-8")
    if layout == "sqlite":
        (work / "db.sqlite3").write_bytes(b"SQLite format 3\x00")
    elif layout == "multi":
        pg = work / "pg_dump"
        pg.mkdir()
        (pg / "manifest.tsv").write_text(
            "pasarguard\tpasarguard\t1\tdump.sql\t2.28.1\n", encoding="utf-8"
        )
        (pg / "dump.sql").write_text("-- dump\n", encoding="utf-8")
    else:
        (work / "db_backup.sql").write_text("-- dump\n", encoding="utf-8")
    zpath = dest / "backup.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in work.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(work).as_posix())
    return zpath


def test_analyze_newer_timescale_backup_reports_floor():
    """Reproduce the reported archive: 2.28.x dump, no version metadata anywhere.

    The wizard used to see only the chunk catalog era (schema_name, i.e. "pre-2.29")
    and conclude a 2.26.4 server was compatible, then fail inside psql.
    """
    import tempfile
    import shutil
    import app.services.pg_restore as mod

    base = Path(tempfile.mkdtemp(prefix="pg-restore-ts-floor-"))
    work = base / "content"
    (work / "pg_dump").mkdir(parents=True)
    (work / ".env").write_text(
        'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://u:p@timescaledb:5432/pasarguard"\n'
        'DB_PASSWORD="x"\n',
        encoding="utf-8",
    )
    # Empty version column, exactly like the archive that failed
    (work / "pg_dump" / "manifest.tsv").write_text(
        "pasarguard\tpasarguard\t1\tpasarguard.sql\t\n", encoding="utf-8"
    )
    (work / "pg_dump" / "pasarguard.sql").write_text(
        "COPY _timescaledb_catalog.chunk (id, hypertable_id, schema_name, table_name) "
        "FROM stdin;\n1\t1\t_timescaledb_internal\t_hyper_1_1_chunk\n\\.\n"
        + CONTINUOUS_AGG_228_COPY
        + "\\.\n",
        encoding="utf-8",
    )
    z = base / "backup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in work.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(work).as_posix())

    orig_installed = mod.is_pasarguard_installed
    orig_db = mod.get_pasarguard_db_type
    mod.is_pasarguard_installed = lambda: True  # type: ignore
    mod.get_pasarguard_db_type = lambda: "timescaledb"  # type: ignore
    try:
        a = analyze_pasarguard_backup(path=z)
        assert a["backup_db"] == "timescaledb"
        assert a["layout"] == "multi"
        assert a["ok"] is True
        assert a["timescaledb_versions"] == []
        assert a["timescaledb_chunk_catalog"] == "schema_name"
        assert a["timescaledb_min_version"] == "2.28.0"
        # End to end: a 2.26.4 server now gets pinned instead of failing mid-restore
        assert (
            resolve_wanted_ts_for_live(
                parse_timescale_wanted(a["timescaledb_versions"]),
                live_ver="2.26.4",
                catalog_era=a["timescaledb_chunk_catalog"],
                min_ver=a["timescaledb_min_version"],
            )
            == TS_LAST_SCHEMA_NAME_CHUNK
        )
    finally:
        mod.is_pasarguard_installed = orig_installed  # type: ignore
        mod.get_pasarguard_db_type = orig_db  # type: ignore
        shutil.rmtree(base, ignore_errors=True)
    print("OK: analyze reports timescale floor for newer backup")


def test_analyze_all_db_types():
    """Analyze each DB family zip without requiring PasarGuard installed."""
    import tempfile
    import shutil
    import app.services.pg_restore as mod

    base = Path(tempfile.mkdtemp(prefix="pg-restore-zips-"))
    cases = [
        ("sqlite", "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3", "sqlite"),
        ("mysql", "mysql+asyncmy://u:p@127.0.0.1/pasarguard", "single"),
        ("mariadb", "mariadb+asyncmy://u:p@127.0.0.1/pasarguard", "single"),
        ("postgresql", "postgresql+asyncpg://u:p@127.0.0.1/pasarguard", "single"),
        ("timescaledb", "postgresql+asyncpg://u:p@timescaledb:5432/pasarguard", "multi"),
    ]

    orig_installed = mod.is_pasarguard_installed
    orig_db = mod.get_pasarguard_db_type
    mod.is_pasarguard_installed = lambda: True  # type: ignore
    try:
        for name, url, layout in cases:
            mod.get_pasarguard_db_type = lambda n=name: n  # type: ignore
            z = _make_backup_zip(base / name, url, layout=layout)
            a = analyze_pasarguard_backup(path=z)
            assert a["backup_db"]
            assert a["layout"] in ("sqlite_file", "single", "multi")
            assert a["ok"] is True
            print(f"OK: analyze {name} layout={a['layout']} backup_db={a['backup_db']}")
    finally:
        mod.is_pasarguard_installed = orig_installed  # type: ignore
        mod.get_pasarguard_db_type = orig_db  # type: ignore
        shutil.rmtree(base, ignore_errors=True)


def test_analyze_experimental_hard_mismatch():
    import tempfile
    import shutil
    import app.services.pg_restore as mod

    base = Path(tempfile.mkdtemp(prefix="pg-restore-mismatch-"))
    z = _make_backup_zip(base, "mysql+asyncmy://u:p@127.0.0.1/pasarguard", layout="single")
    orig_installed = mod.is_pasarguard_installed
    orig_db = mod.get_pasarguard_db_type
    mod.is_pasarguard_installed = lambda: True  # type: ignore
    mod.get_pasarguard_db_type = lambda: "timescaledb"  # type: ignore
    try:
        a = analyze_pasarguard_backup(path=z)
        assert a["ok"] is True
        assert a["experimental_db_change"] is True
        assert a["soft_match"] is False
        print("OK: experimental hard mismatch flagged")
    finally:
        mod.is_pasarguard_installed = orig_installed  # type: ignore
        mod.get_pasarguard_db_type = orig_db  # type: ignore
        shutil.rmtree(base, ignore_errors=True)

def test_filter_globals_sql_makes_create_role_idempotent():
    """filter_globals_sql must wrap CREATE ROLE in a DO-block so duplicate roles are tolerated."""
    sample = (
        "-- pg_dumpall globals\n"
        "CREATE ROLE pasarguard;\n"
        "ALTER ROLE pasarguard WITH NOSUPERUSER NOCREATEDB LOGIN;\n"
        "CREATE ROLE postgres SUPERUSER;\n"
    )
    result = filter_globals_sql(sample)
    # CREATE ROLE must be wrapped inside DO-blocks, not as bare top-level statements
    assert "DO $pg_restore_idempotent$" in result
    assert "duplicate_object" in result
    # The DO-block delimiter must appear at least as many times as there are CREATE ROLE stmts
    assert result.count("$pg_restore_idempotent$") >= 4  # 2 roles × open+close
    # No line starting with CREATE ROLE at column 0 (bare, outside a DO block)
    for line in result.splitlines():
        assert not line.startswith("CREATE ROLE"), (
            f"Bare CREATE ROLE found at column 0: {line!r}"
        )
    # Non-role lines must be preserved
    assert "-- pg_dumpall globals" in result
    assert "ALTER ROLE pasarguard WITH NOSUPERUSER NOCREATEDB LOGIN;" in result


def test_verify_settings_soft_when_critical_ok():
    """settings 1/16 must not fail restore when users/hosts/… match (dump estimator noise)."""
    import asyncio
    from unittest.mock import patch

    from app.services.migrators.base import MigrationJob
    from app.services import pg_restore

    counts = {
        "users": "69",
        "hosts": "15",
        "groups": "2",
        "nodes": "2",
        "inbounds": "3",
        "admins": "2",
        "settings": "1",
        "users_groups_association": "70",
        "inbounds_groups_association": "3",
        "core_configs": "2",
    }
    expected = {k: int(v) if k != "settings" else 16 for k, v in counts.items()}
    # expected settings=16, actual=1 via counts

    async def fake_run(job, cmd, cwd=None, timeout=600):
        text = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        for table, n in counts.items():
            if f"FROM `{table}`" in text or f'FROM "{table}"' in text:
                return True, f"{n}\n"
        return False, "no"

    async def _run():
        job = MigrationJob(job_id="verify1")
        with patch.object(pg_restore, "_run", fake_run), \
             patch.object(pg_restore, "_detect_db_container", return_value="mysql"), \
             patch.object(pg_restore, "PASARGUARD_DIR", Path("/opt/pasarguard")):
            actual = await pg_restore._verify_restored_data(
                job,
                "mysql",
                "secret",
                "pasarguard",
                "pasarguard",
                expected,
                require_any_data=True,
            )
        assert actual["users"] == 69
        assert actual["settings"] == 1
        assert any("soft" in (ln or "").lower() or "settings" in (ln or "").lower()
                   for ln in job.logs)

    asyncio.run(_run())
    print("OK: settings soft verify when critical tables match")


def test_verify_users_gap_still_hard_fails():
    import asyncio
    from unittest.mock import patch

    from app.services.migrators.base import MigrationJob
    from app.services import pg_restore

    async def fake_run(job, cmd, cwd=None, timeout=600):
        text = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "FROM `users`" in text:
            return True, "10\n"
        if "FROM `hosts`" in text:
            return True, "15\n"
        return True, "0\n"

    async def _run():
        job = MigrationJob(job_id="verify2")
        with patch.object(pg_restore, "_run", fake_run), \
             patch.object(pg_restore, "_detect_db_container", return_value="mysql"), \
             patch.object(pg_restore, "PASARGUARD_DIR", Path("/opt/pasarguard")):
            try:
                await pg_restore._verify_restored_data(
                    job,
                    "mysql",
                    "secret",
                    "pasarguard",
                    "pasarguard",
                    {"users": 69, "hosts": 15},
                    require_any_data=True,
                )
                raise AssertionError("expected RuntimeError for users gap")
            except RuntimeError as e:
                assert "users: 10/69" in str(e)

    asyncio.run(_run())
    print("OK: users gap still hard-fails")


MYSQL_PANEL_DUMP = (
    "-- MySQL dump 10.13\n"
    "CREATE TABLE `users` (\n"
    "  `id` int NOT NULL,\n"
    "  `username` varchar(64)\n"
    ") ENGINE=InnoDB;\n"
    "INSERT INTO `users` VALUES (1,'alice');\n"
    "CREATE TABLE `hosts` (`id` int) ENGINE=InnoDB;\n"
)


def _zip_tree(base: Path, mapping: dict[str, bytes | str]) -> Path:
    zpath = base / "backup.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for name, content in mapping.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(name, data)
    return zpath


def _analyze_zip(z: Path, installed_db: str = "mysql"):
    import app.services.pg_restore as mod

    orig_installed = mod.is_pasarguard_installed
    orig_db = mod.get_pasarguard_db_type
    mod.is_pasarguard_installed = lambda: True  # type: ignore
    mod.get_pasarguard_db_type = lambda: installed_db  # type: ignore
    try:
        return analyze_pasarguard_backup(path=z)
    finally:
        mod.is_pasarguard_installed = orig_installed  # type: ignore
        mod.get_pasarguard_db_type = orig_db  # type: ignore


def test_discover_third_party_mysql_named_dump():
    import tempfile
    import shutil

    td = Path(tempfile.mkdtemp(prefix="pg-discover-mysql-"))
    try:
        (td / "opt" / "pasarguard").mkdir(parents=True)
        (td / "opt" / "pasarguard" / ".env").write_text(
            'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://u:p@127.0.0.1/pasarguard"\n'
            'DB_PASSWORD="x"\n',
            encoding="utf-8",
        )
        (td / "pasarguard.sql").write_text(MYSQL_PANEL_DUMP, encoding="utf-8")
        art = discover_backup_artifacts(td, env_db="mysql")
        assert art["layout"] == "single"
        assert Path(art["dump_path"]).name == "pasarguard.sql"
        assert art["dump_engine"] in ("mysql", "mariadb")
        print("OK: discover third-party mysql dump by content")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_analyze_third_party_netb_style_zip():
    """netb_backuper-style zip: .env under opt/, dump not named db_backup.sql."""
    import tempfile
    import shutil

    td = Path(tempfile.mkdtemp(prefix="pg-netb-"))
    try:
        z = _zip_tree(td, {
            "opt/pasarguard/.env": (
                'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://u:p@127.0.0.1/pasarguard"\n'
                'MYSQL_ROOT_PASSWORD="x"\n'
                'DB_PASSWORD="x"\n'
            ),
            "0826-1130.sql": MYSQL_PANEL_DUMP,
            "var/lib/pasarguard/xray_config.json": "{}",
        })
        a = _analyze_zip(z, "mysql")
        assert a["ok"] is True, a.get("warnings")
        assert a["layout"] == "single"
        assert a["backup_db"] == "mysql"
        assert a["dump_name"] == "0826-1130.sql"
        print("OK: analyze netb-style zip")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_analyze_nested_sqlite_var_lib():
    import tempfile
    import shutil
    import sqlite3

    td = Path(tempfile.mkdtemp(prefix="pg-sqlite-nested-"))
    try:
        db_bytes_path = td / "tmp.db"
        conn = sqlite3.connect(str(db_bytes_path))
        conn.execute("CREATE TABLE users (id INTEGER)")
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.commit()
        conn.close()
        z = _zip_tree(td, {
            "opt/pasarguard/.env": (
                'SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"\n'
            ),
            "var/lib/pasarguard/db.sqlite3": db_bytes_path.read_bytes(),
        })
        a = _analyze_zip(z, "sqlite")
        assert a["ok"] is True, a.get("warnings")
        assert a["layout"] == "sqlite_file"
        assert a["backup_db"] == "sqlite"
        print("OK: analyze nested sqlite var/lib zip")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_analyze_mysql_env_without_sql_dump_fails():
    """Folder-only bot backup of a MySQL panel must still fail (no mysqldump)."""
    import tempfile
    import shutil

    td = Path(tempfile.mkdtemp(prefix="pg-mysql-nodump-"))
    try:
        z = _zip_tree(td, {
            "opt/pasarguard/.env": (
                'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://u:p@127.0.0.1/pasarguard"\n'
                'DB_PASSWORD="x"\n'
            ),
            "var/lib/pasarguard/db.sqlite3": b"SQLite format 3\x00leftover",
            "opt/pasarguard/docker-compose.yml": "services: {}\n",
        })
        a = _analyze_zip(z, "mysql")
        assert a["ok"] is False
        assert a["layout"] == "none"
        msgs = " ".join(w.get("fa") or w.get("en") or "" for w in a.get("warnings") or [])
        assert "دامپ" in msgs or "dump" in msgs.lower()
        print("OK: mysql env without sql dump still fails")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_analyze_sql_gz_dump():
    import gzip
    import tempfile
    import shutil

    td = Path(tempfile.mkdtemp(prefix="pg-sqlgz-"))
    try:
        gz = gzip.compress(MYSQL_PANEL_DUMP.encode("utf-8"))
        z = _zip_tree(td, {
            ".env": (
                'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://u:p@127.0.0.1/pasarguard"\n'
                'DB_PASSWORD="x"\n'
            ),
            "backup.sql.gz": gz,
        })
        a = _analyze_zip(z, "mysql")
        assert a["ok"] is True, a.get("warnings")
        assert a["layout"] == "single"
        assert a["dump_name"] == "backup.sql.gz"
        print("OK: analyze .sql.gz dump")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_discover_ignores_xui_sqlite():
    import tempfile
    import shutil
    import sqlite3

    td = Path(tempfile.mkdtemp(prefix="pg-xui-"))
    try:
        db = td / "x-ui.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE inbounds (id INTEGER)")
        conn.execute("CREATE TABLE client_traffics (id INTEGER)")
        conn.commit()
        conn.close()
        art = discover_backup_artifacts(td, env_db="mysql")
        assert art["layout"] == "none"
        print("OK: 3x-ui sqlite is not treated as PasarGuard dump")
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    test_soft_db_family_matrix()
    test_ts_to_ts_syncs_alembic_before_panel()
    test_ensure_timescaledb_forces_post_restore_when_on()
    test_ensure_timescaledb_forces_post_restore_when_check_fails()
    test_filter_timescaledb_extension_sql()
    test_filter_timescaledb_strip_all_for_plain_pg()
    test_parse_timescale_wanted()
    test_detect_ts_mismatch_from_official_error()
    test_is_ts_catalog_mismatch_error_schema_name()
    test_detect_dump_chunk_catalog_era()
    test_detect_dump_ts_catalog_floor()
    test_detect_dump_ts_catalog_floor_new_tables()
    test_detect_backup_ts_catalog_floor_multi_layout()
    test_ts_floor_from_error_text()
    test_is_ts_catalog_mismatch_error_continuous_agg()
    test_resolve_wanted_ts_for_live_pins_pre_229()
    test_resolve_wanted_ts_for_live_honours_dump_floor()
    test_ts_pin_for_floor()
    test_wanted_ts_for_restore_retry_from_catalog_error()
    test_wanted_ts_for_restore_retry_newer_backup()
    test_align_image_failed_pull_keeps_data_and_tag()
    test_explain_newer_ts_backup_error()
    test_explain_schema_name_chunk_error()
    test_collect_backup_ts_from_compose_and_catalog()
    test_is_auth_failure_text()
    test_sql_literal_escapes_quotes()
    test_merge_env_preserves_password()
    test_parse_manifest_ts_versions()
    test_analyze_newer_timescale_backup_reports_floor()
    test_analyze_all_db_types()
    test_analyze_experimental_hard_mismatch()
    test_filter_globals_sql_makes_create_role_idempotent()
    test_verify_settings_soft_when_critical_ok()
    test_verify_users_gap_still_hard_fails()
    test_discover_third_party_mysql_named_dump()
    test_analyze_third_party_netb_style_zip()
    test_analyze_nested_sqlite_var_lib()
    test_analyze_mysql_env_without_sql_dump_fails()
    test_analyze_sql_gz_dump()
    test_discover_ignores_xui_sqlite()
    print("\nAll pg_restore tests passed")
