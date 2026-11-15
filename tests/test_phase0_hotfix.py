"""Phase 0 hotfixes: env last-wins, token redaction, wizard uninstall scope."""

from app.services.env_migration import detect_db_type_from_env, read_env_var


def test_read_env_var_last_active_assignment_wins():
    text = (
        'SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"\n'
        'SQLALCHEMY_DATABASE_URL="mysql+pymysql://root:password@127.0.0.1/marzban"\n'
    )
    assert read_env_var(text, "SQLALCHEMY_DATABASE_URL").startswith("mysql+pymysql://")
    assert detect_db_type_from_env(text, prefer_compose=False) == "mysql"


def test_read_env_var_ignores_commented_and_empty_later_lines():
    text = (
        'SQLALCHEMY_DATABASE_URL="mysql+pymysql://root:p@127.0.0.1/marzban"\n'
        '# SQLALCHEMY_DATABASE_URL = "sqlite:////x"\n'
        "SQLALCHEMY_DATABASE_URL=\n"
    )
    assert read_env_var(text, "SQLALCHEMY_DATABASE_URL").startswith("mysql+pymysql://")


def test_read_env_var_single_line_unchanged():
    assert read_env_var('A="x y"\n', "A") == "x y"
    assert read_env_var("B=1 # c\n", "B") == "1"
    assert read_env_var("C=1\n", "MISSING") is None


def test_redact_telegram_token():
    from app.services.backup_telegram import redact_telegram_text

    msg = "Client error for url 'https://api.telegram.org/bot123456789:AAE-abcdefghijklmnopqrstuvwxyz12345/getMe'"
    out = redact_telegram_text(msg)
    assert "AAE-" not in out and "bot<redacted>" in out
    assert "AAE-abcdefghijklmnopqrstuvwxyz12345" not in redact_telegram_text(
        "token 123456789:AAE-abcdefghijklmnopqrstuvwxyz12345 bad"
    )


def test_self_uninstall_keeps_backup_service():
    from app.services import self_uninstall

    prev = self_uninstall.uninstall_preview()
    joined = " ".join(prev["commands"])
    assert prev["backup_service_kept"] is True
    assert "pg-backup" not in joined
    assert "mv " in joined


def test_session_secret_survives_whitespace_edge_bytes(tmp_path, monkeypatch):
    import app.services.backup_auth as auth

    secret_file = tmp_path / ".session_secret"
    secret_file.write_bytes(b"\n" + b"A" * 30 + b"\n")  # 32 bytes, whitespace at both ends
    monkeypatch.setattr(auth, "BACKUP_SECRET_FILE", secret_file)
    assert auth.get_session_secret() == b"\n" + b"A" * 30 + b"\n"


def test_read_env_var_empty_assignment_does_not_swallow_next_line():
    text = "SQLALCHEMY_DATABASE_URL=\nUVICORN_PORT=8000\n"
    assert read_env_var(text, "SQLALCHEMY_DATABASE_URL") is None
    assert read_env_var(text, "UVICORN_PORT") == "8000"


def test_read_env_var_case_and_export():
    text = (
        'SQLALCHEMY_DATABASE_URL="mysql+pymysql://root:p@127.0.0.1/marzban"\n'
        'sqlalchemy_database_url = "sqlite:////old"\n'
    )
    # exact-case key wins over a lowercase leftover
    assert read_env_var(text, "SQLALCHEMY_DATABASE_URL").startswith("mysql+pymysql://")
    assert read_env_var('export A="1"\n', "A") == "1"
    # a differently-cased key is still used when there is no exact match
    assert read_env_var("sqlalchemy_database_url=x\n", "SQLALCHEMY_DATABASE_URL") == "x"


def _run_uninstall_script(tmp_path, *, keep_root, backup_unit_text):
    import os
    import subprocess

    from app.services import self_uninstall as su

    inst = tmp_path / "opt" / "pg-migrator"
    (inst / "backups").mkdir(parents=True)
    (inst / "backups" / "a.zip").write_text("x")
    unit = tmp_path / "pg-migrator.service"
    unit.write_text("x")
    bunit = tmp_path / "pg-backup.service"
    bunit.write_text(backup_unit_text.replace("__INST__", str(inst)))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(bindir / "systemctl", 0o755)
    script = su._UNINSTALL_SCRIPT
    for k, v in (
        ("__INSTALL_DIR__", str(inst)), ("__KEEP_ROOT__", str(keep_root)),
        ("__SERVICE__", "pg-migrator"), ("__UNIT__", str(unit)),
        ("__BACKUP_UNIT__", str(bunit)), ("__TMP__", str(scratch)),
    ):
        script = script.replace(k, v)
    f = tmp_path / "u.sh"
    f.write_text(script)
    subprocess.run(["bash", str(f)], env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}, check=True)
    import time

    time.sleep(1.5)  # the final rm runs detached
    return inst


def test_uninstall_moves_backups_then_removes_wizard(tmp_path):
    keep = tmp_path / "keep"
    inst = _run_uninstall_script(
        tmp_path, keep_root=keep, backup_unit_text="[Service]\nWorkingDirectory=/opt/pg-backup\n",
    )
    assert not inst.exists()
    assert list(keep.rglob("a.zip"))


def test_uninstall_keeps_everything_when_backups_cannot_be_moved(tmp_path):
    bad_root = tmp_path / "file-not-dir"
    bad_root.write_text("x")
    inst = _run_uninstall_script(
        tmp_path, keep_root=bad_root, backup_unit_text="[Service]\nWorkingDirectory=/opt/pg-backup\n",
    )
    assert (inst / "backups" / "a.zip").exists()


def test_uninstall_detects_legacy_combined_install_with_quotes_and_slash(tmp_path):
    inst = _run_uninstall_script(
        tmp_path, keep_root=tmp_path / "keep",
        backup_unit_text='[Service]\nWorkingDirectory="__INST__/"\n',
    )
    assert inst.exists()
