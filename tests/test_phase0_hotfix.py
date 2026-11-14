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
    assert "cp -a" in joined


def test_session_secret_survives_whitespace_edge_bytes(tmp_path, monkeypatch):
    import app.services.backup_auth as auth

    secret_file = tmp_path / ".session_secret"
    secret_file.write_bytes(b"\n" + b"A" * 30 + b"\n")  # 32 bytes, whitespace at both ends
    monkeypatch.setattr(auth, "BACKUP_SECRET_FILE", secret_file)
    assert auth.get_session_secret() == b"\n" + b"A" * 30 + b"\n"
