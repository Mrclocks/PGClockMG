"""Tests for alembic revision parsing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.native_migration.source_version import (
    alembic_revisions_for_stamp,
    normalize_alembic_revision,
    normalize_alembic_revisions,
)


def test_normalize_from_docker_noise():
    raw = (
        'time="2026-07-22T09:08:31Z" level=warning msg="The PGADMIN_EMAIL variable is not set"\n'
        'time="2026-07-22T09:08:31Z" level=warning msg="The PGADMIN_PASSWORD variable is not set"\n'
        "f976bfcf4738\n"
    )
    assert normalize_alembic_revision(raw) == "f976bfcf4738"
    print("OK: normalize from docker noise")


def test_normalize_head_returns_none():
    assert normalize_alembic_revision("head") is None
    assert normalize_alembic_revision("  HEAD  ") is None
    print("OK: head sentinel")


def test_multi_revision_extract():
    text = "c9b48df42f10\nd4f8c1b2a9e3\n9aa99aaee80f"
    revs = normalize_alembic_revisions(text)
    assert revs == ["c9b48df42f10", "d4f8c1b2a9e3", "9aa99aaee80f"]
    assert alembic_revisions_for_stamp(text) == revs
    print("OK: multi revision extract")


def test_revision_max_32_chars():
    long_hex = "a" * 40
    assert normalize_alembic_revision(long_hex) == "a" * 32
    print("OK: revision truncated to 32")


if __name__ == "__main__":
    test_normalize_from_docker_noise()
    test_normalize_head_returns_none()
    test_multi_revision_extract()
    test_revision_max_32_chars()
    print("\nAll alembic revision tests passed")
