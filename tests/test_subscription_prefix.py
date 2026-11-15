"""Tests for subscription public-base helpers (redirect + settings + certbot soft)."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.subscription_prefix import (
    apply_subscription_prefix_sqlite,
    is_plausible_hostname,
    manual_cert_guide,
    normalize_public_base,
    try_certbot_issue,
    write_subscription_prefix_env,
)


def test_normalize_public_base_from_parts():
    assert normalize_public_base("sub.example.com", 8443, "https") == (
        "https://sub.example.com:8443"
    )
    assert normalize_public_base(
        "https://sub.example.com:2096/path", None, None, fallback="http://x"
    ) == "https://sub.example.com:2096"
    assert normalize_public_base("", None, None, fallback="https://1.2.3.4:8000") == (
        "https://1.2.3.4:8000"
    )
    print("OK: normalize_public_base")


def test_write_env_and_sqlite_settings(tmp_path: Path | None = None):
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        env = td_path / ".env"
        env.write_text("UVICORN_PORT=8000\n", encoding="utf-8")
        assert write_subscription_prefix_env(
            "https://sub.example.com:8000", env_path=env,
        )
        text = env.read_text(encoding="utf-8")
        assert "SUBSCRIPTION_URL_PREFIX" in text
        assert "https://sub.example.com:8000" in text

        db_path = td_path / "db.sqlite3"
        db = sqlite3.connect(str(db_path))
        db.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)"
        )
        db.execute(
            "INSERT INTO settings VALUES (1, 'subscription', ?)",
            (json.dumps({"other": 1}),),
        )
        db.commit()
        db.close()
        assert apply_subscription_prefix_sqlite(db_path, "https://sub.example.com:8000")
        db = sqlite3.connect(str(db_path))
        val = db.execute(
            "SELECT value FROM settings WHERE key='subscription'"
        ).fetchone()[0]
        db.close()
        data = json.loads(val)
        assert data["url_prefix"] == "https://sub.example.com:8000"
        assert data["other"] == 1
    print("OK: env + sqlite settings url_prefix")


def test_certbot_missing_is_soft_skip():
    with patch("app.services.subscription_prefix.shutil.which", return_value=None):
        out = try_certbot_issue(["sub.example.com", "panel.example.com"])
    assert out["ok"] is False
    assert out["skipped"] is True
    assert "certbot" in (out.get("error") or "").lower()
    assert is_plausible_hostname("sub.example.com")
    assert not is_plausible_hostname("not a host")
    guide = manual_cert_guide(["sub.example.com"], lang="fa")
    assert guide and "certs" in guide[1]
    print("OK: certbot soft-skip when missing")


if __name__ == "__main__":
    test_normalize_public_base_from_parts()
    test_write_env_and_sqlite_settings()
    test_certbot_missing_is_soft_skip()
    print("\nAll subscription_prefix tests passed.")


def test_url_prefix_written_to_real_single_row_schema(tmp_path):
    """PasarGuard stores settings as ONE row with a JSON column per section."""
    db_path = tmp_path / "db.sqlite3"
    db = sqlite3.connect(str(db_path))
    db.execute(
        "CREATE TABLE settings (id INTEGER PRIMARY KEY, telegram JSON, subscription JSON, general JSON)"
    )
    db.execute(
        "INSERT INTO settings VALUES (1, '{}', ?, '{\"x\": 1}')",
        (json.dumps({"url_prefix": "", "support_url": "https://t.me/"}),),
    )
    db.commit()
    db.close()

    assert apply_subscription_prefix_sqlite(db_path, "https://sub.example.com:8000/")
    db = sqlite3.connect(str(db_path))
    sub, general = db.execute("SELECT subscription, general FROM settings WHERE id=1").fetchone()
    db.close()
    data = json.loads(sub)
    assert data["url_prefix"] == "https://sub.example.com:8000"
    assert data["support_url"] == "https://t.me/"  # other keys preserved
    assert json.loads(general) == {"x": 1}  # other sections untouched


def test_url_prefix_real_schema_without_row_is_noop(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, subscription JSON)")
    db.commit()
    db.close()
    assert apply_subscription_prefix_sqlite(db_path, "https://s.example.com") is False


def _real_db(path, subscription_json):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, subscription JSON)")
    if subscription_json is not None:
        db.execute("INSERT INTO settings VALUES (1, ?)", (subscription_json,))
    db.commit()
    db.close()


def test_corrupt_subscription_json_is_not_replaced(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    _real_db(db_path, "{not json")
    logs: list[str] = []
    assert apply_subscription_prefix_sqlite(db_path, "https://s.example.com", log=logs.append) is False
    db = sqlite3.connect(str(db_path))
    assert db.execute("SELECT subscription FROM settings").fetchone()[0] == "{not json"
    db.close()
    assert any("not valid JSON" in m for m in logs)


def test_missing_row_and_overwritten_prefix_are_logged(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    _real_db(db_path, None)
    logs: list[str] = []
    assert apply_subscription_prefix_sqlite(db_path, "https://s.example.com", log=logs.append) is False
    assert any("settings row not found" in m for m in logs)

    db_path2 = tmp_path / "db2.sqlite3"
    _real_db(db_path2, json.dumps({"url_prefix": "https://old.example.com", "keep": 1}))
    logs = []
    assert apply_subscription_prefix_sqlite(db_path2, "https://new.example.com", log=logs.append) is True
    assert any("overwritten" in m and "old.example.com" in m for m in logs)
    db = sqlite3.connect(str(db_path2))
    data = json.loads(db.execute("SELECT subscription FROM settings").fetchone()[0])
    db.close()
    assert data == {"url_prefix": "https://new.example.com", "keep": 1}
