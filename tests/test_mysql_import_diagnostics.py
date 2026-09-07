"""Unit tests for MySQL import failure classification / message formatting."""

from pathlib import Path

from app.services.mysql_import_diagnostics import (
    MYSQL_IMPORT_SESSION_PREAMBLE,
    assess_mysql_import_ram,
    classify_mysql_import_failure,
    format_mysql_import_error,
    mysql_import_session_preamble_sql,
    write_mysql_import_stdin_file,
)


def test_exit_137_is_killed_not_credentials():
    failure = classify_mysql_import_failure(
        137,
        "mysql: [Warning] Using a password on the command line interface can be insecure.",
    )
    assert failure.kind == "killed"
    msg = format_mysql_import_error(
        failure,
        output_tail="mysql: [Warning] Using a password on the command line interface can be insecure.",
    )
    assert "Check DB credentials" not in msg
    assert "can be insecure" not in msg
    low = msg.lower()
    assert "not" in low and ("password" in low or "credential" in low)
    assert "137" in msg or "sigkill" in low


def test_exit_137_with_container_died_and_oom():
    failure = classify_mysql_import_failure(
        137, "", container_died=True, oom_killed=True,
    )
    assert failure.kind == "killed"
    msg = format_mysql_import_error(
        failure, diag_tail="OOMKilled: true\ncompose ps: mysql Exited",
    )
    assert "OOM" in msg
    assert "container diagnostics:" in msg
    assert "not a db password problem" in msg.lower()


def test_access_denied_is_auth():
    failure = classify_mysql_import_failure(
        1, "ERROR 1045 (28000): Access denied for user 'root'@'localhost'",
    )
    assert failure.kind == "auth"
    msg = format_mysql_import_error(
        failure,
        output_tail="ERROR 1045 (28000): Access denied for user 'root'@'localhost'",
    )
    assert "1045" in msg
    assert "MYSQL_ROOT_PASSWORD" in msg or "credentials" in msg.lower()


def test_sql_syntax_error_classified():
    failure = classify_mysql_import_failure(
        1, "ERROR 1064 (42000): You have an error in your SQL syntax",
    )
    assert failure.kind == "sql"
    msg = format_mysql_import_error(
        failure, output_tail="ERROR 1064 (42000): You have an error",
    )
    assert "SQL" in msg


def test_connection_refused_is_client_not_sql():
    failure = classify_mysql_import_failure(
        1, "ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1'",
    )
    assert failure.kind == "client"
    msg = format_mysql_import_error(
        failure,
        output_tail="ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1'",
    )
    assert "Can't connect" in msg
    assert "Check DB credentials" not in msg


def test_generic_nonzero_keeps_client_output():
    failure = classify_mysql_import_failure(1, "Unknown failure from mysql client")
    assert failure.kind == "client"
    msg = format_mysql_import_error(
        failure, output_tail="Unknown failure from mysql client",
    )
    assert "Unknown failure from mysql client" in msg
    assert "Check DB credentials" not in msg


def test_sigterm_143_is_killed():
    failure = classify_mysql_import_failure(143, "")
    assert failure.kind == "killed"


def test_negative_sigkill_code():
    failure = classify_mysql_import_failure(-9, "")
    assert failure.kind == "killed"


def test_format_includes_guidance_about_retry_recreate():
    failure = classify_mysql_import_failure(
        137, "", container_died=True, oom_killed=True,
    )
    msg = format_mysql_import_error(failure)
    assert "recreate" in msg.lower() or "retry" in msg.lower()


def test_ram_preflight_ok_for_small_dump_with_gb_free():
    advice = assess_mysql_import_ram(25 * 1024 * 1024, 2 * 1024 * 1024 * 1024)
    assert advice.level == "ok"
    assert "ok" in advice.message.lower()


def test_ram_preflight_warns_on_very_low_free_ram_but_is_advisory():
    advice = assess_mysql_import_ram(25 * 1024 * 1024, 100 * 1024 * 1024)
    assert advice.level == "warn"
    assert "continuing" in advice.message.lower()


def test_ram_preflight_warns_large_dump_vs_free_ram():
    advice = assess_mysql_import_ram(200 * 1024 * 1024, 400 * 1024 * 1024)
    assert advice.level == "warn"
    assert "continuing" in advice.message.lower()


def test_ram_preflight_unknown_mem_is_info_not_block():
    from app.services import mysql_import_diagnostics as mid

    orig = mid.read_mem_available_bytes
    mid.read_mem_available_bytes = lambda: None
    try:
        advice = assess_mysql_import_ram(10 * 1024 * 1024, None)
    finally:
        mid.read_mem_available_bytes = orig
    assert advice.level == "info"
    assert "unknown" in advice.message.lower()
    assert "continuing" in advice.message.lower()


def test_session_preamble_is_session_only_and_minimal():
    sql = mysql_import_session_preamble_sql()
    assert sql == MYSQL_IMPORT_SESSION_PREAMBLE
    low = sql.lower()
    assert "session" in low
    assert "foreign_key_checks=0" in low
    assert "unique_checks=0" in low
    assert "global" not in low
    assert "sql_log_bin" not in low
    assert "sql_mode" not in low


def test_write_mysql_import_stdin_file_streams_preamble(tmp_path: Path):
    dump = tmp_path / "dump.sql"
    dump.write_bytes(b"INSERT INTO t VALUES (1);\n")
    dest = tmp_path / "ready.sql"
    write_mysql_import_stdin_file(dump, dest)
    data = dest.read_bytes()
    assert data.startswith(MYSQL_IMPORT_SESSION_PREAMBLE.encode("utf-8"))
    assert data.endswith(b"INSERT INTO t VALUES (1);\n")


if __name__ == "__main__":
    test_exit_137_is_killed_not_credentials()
    test_exit_137_with_container_died_and_oom()
    test_access_denied_is_auth()
    test_sql_syntax_error_classified()
    test_connection_refused_is_client_not_sql()
    test_generic_nonzero_keeps_client_output()
    test_sigterm_143_is_killed()
    test_negative_sigkill_code()
    test_format_includes_guidance_about_retry_recreate()
    test_ram_preflight_ok_for_small_dump_with_gb_free()
    test_ram_preflight_warns_on_very_low_free_ram_but_is_advisory()
    test_ram_preflight_warns_large_dump_vs_free_ram()
    test_ram_preflight_unknown_mem_is_info_not_block()
    test_session_preamble_is_session_only_and_minimal()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_write_mysql_import_stdin_file_streams_preamble(Path(d))
    print("OK: mysql_import_diagnostics")
