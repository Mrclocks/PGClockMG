"""Unit tests for PasarGuard install prompt auto-answer helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.pg_installer import (
    _build_cmd,
    _norm,
    _volume_delete_answer,
    _volume_delete_prompt_seen,
)


def test_volume_prompt_seen_official_warning():
    banner = (
        "WARNING: This will PERMANENTLY delete all data in these volumes!"
    )
    assert _volume_delete_prompt_seen(banner)
    print("OK: volume WARNING banner")


def test_volume_prompt_seen_official_question():
    q = "Do you want to delete these volumes? (default: no)"
    assert _volume_delete_prompt_seen(q)
    print("OK: volume question banner")


def test_volume_prompt_seen_read_prompt():
    p = "Delete volumes? [y/N]: "
    assert _volume_delete_prompt_seen(p)
    print("OK: volume read -p prompt")


def test_volume_prompt_not_seen_random():
    assert not _volume_delete_prompt_seen("Fetching compose file for pasarguard")
    assert not _volume_delete_prompt_seen("Docker volume: pasarguard_mysql_data")
    print("OK: volume false positives")


def test_volume_delete_answer():
    assert _volume_delete_answer(False) == "n"
    assert _volume_delete_answer(True) == "y"
    print("OK: volume answer")


def test_norm_strips_ansi_for_matching():
    raw = "\x1b[33mWARNING: This will PERMANENTLY delete all data in these volumes!\x1b[0m"
    assert _volume_delete_prompt_seen(_norm(raw))
    print("OK: norm + ansi volume banner")


def test_build_cmd_no_ssl_prefix():
    params = {"database": "timescaledb", "ssl": False, "domain": "", "ssl_http_port": "80"}
    cmd = _build_cmd(Path("/tmp/pasarguard.sh"), params)
    assert "install" in cmd
    assert "--no-ssl" in cmd
    assert "--database" in cmd
    assert cmd[-3:] == ["--database", "timescaledb", "--no-ssl"] or cmd[-1] == "--no-ssl"
    print("OK: build_cmd flags")


if __name__ == "__main__":
    test_volume_prompt_seen_official_warning()
    test_volume_prompt_seen_official_question()
    test_volume_prompt_seen_read_prompt()
    test_volume_prompt_not_seen_random()
    test_volume_delete_answer()
    test_norm_strips_ansi_for_matching()
    test_build_cmd_no_ssl_prefix()
    print("\nAll pg_installer tests passed")
