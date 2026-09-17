"""Tests for Marzban migrate auto-heal helpers (no Docker)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_merge_copy_tree_and_safe_replace_on_collision():
    from app.services.marzban_migrate_heal import merge_copy_tree, safe_replace_tree

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        src = td_path / "src"
        dst = td_path / "dst"
        src.mkdir()
        (src / "fullchain.pem").write_text("CERT", encoding="utf-8")
        (src / "nested").mkdir()
        (src / "nested" / "a.txt").write_text("A", encoding="utf-8")
        # Destination blocked by a file where a dir is needed — merge still works
        dst.mkdir()
        (dst / "keep.txt").write_text("KEEP", encoding="utf-8")
        n = merge_copy_tree(src, dst)
        assert n >= 2
        assert (dst / "fullchain.pem").read_text(encoding="utf-8") == "CERT"
        assert (dst / "keep.txt").read_text(encoding="utf-8") == "KEEP"

        logs: list[str] = []
        # Force replace path that would fail naive rmtree on busy tree: pre-create
        # a file named like a directory child — safe_replace_tree falls back.
        blocked = td_path / "blocked"
        blocked.mkdir()
        (blocked / "fullchain.pem").write_text("OLD", encoding="utf-8")
        how = safe_replace_tree(src, blocked, log=logs.append)
        assert how in ("replaced", "merged")
        assert (blocked / "fullchain.pem").read_text(encoding="utf-8") == "CERT"
    print("OK: merge_copy_tree / safe_replace_tree")


def test_normalize_templates_rename_and_merge():
    from app.services.marzban_migrate_heal import normalize_templates_layout

    with tempfile.TemporaryDirectory() as td:
        templates = Path(td) / "templates"
        v2ray = templates / "v2ray"
        v2ray.mkdir(parents=True)
        (v2ray / "user.html").write_text("U", encoding="utf-8")
        logs: list[str] = []
        assert normalize_templates_layout(templates, log=logs.append) == "renamed"
        assert (templates / "xray" / "user.html").is_file()
        assert not (templates / "v2ray").exists()

        # Both exist → merge
        v2ray.mkdir(parents=True)
        (v2ray / "extra.html").write_text("E", encoding="utf-8")
        assert normalize_templates_layout(templates, log=logs.append) == "merged"
        assert (templates / "xray" / "extra.html").read_text(encoding="utf-8") == "E"
        assert not (templates / "v2ray").exists()
    print("OK: templates layout heal")


def test_transient_infra_classification():
    from app.services.marzban_migrate_heal import is_transient_infra_error

    assert is_transient_infra_error("connection refused while connecting to upstream")
    assert is_transient_infra_error(RuntimeError("mysql did not become ready in time"))
    assert is_transient_infra_error("password authentication failed for user pasarguard")
    assert not is_transient_infra_error(
        "Convert produced empty target but SQLite source still has users=12"
    )
    assert not is_transient_infra_error(
        "Migration incomplete — critical tables were not fully copied"
    )
    print("OK: transient vs hard-fail classification")


def test_hard_fail_asserts_still_raise():
    """Completeness guards must remain hard-fail (auto-heal must not touch them)."""
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    m = MarzbanMigrator(MigrationJob(job_id="assert"), {})
    try:
        m._abort_if_empty_convert(
            Path("/nonexistent"),
            {"users": 0, "inbounds": 0, "admins": 0, "hosts": 0, "nodes": 0, "groups": 0},
        )
    except RuntimeError:
        raise AssertionError("empty stats with missing sqlite should not raise")
    try:
        m._abort_if_inbounds_missing_from_stats({"users": 5, "inbounds": 0})
        raise AssertionError("expected hard-fail for users without inbounds")
    except RuntimeError as e:
        assert "inbounds=0" in str(e)
    try:
        m._assert_pasarguard_shape_ready(
            {"users": 3, "hosts": 1, "inbounds": 0, "core_configs": 0},
            tables_present={"users", "hosts", "inbounds", "core_configs"},
            engine="sqlite",
        )
        raise AssertionError("expected hard-fail for empty inbounds")
    except RuntimeError as e:
        assert "inbounds empty" in str(e)
    print("OK: hard-fail completeness asserts intact")


def test_safe_start_with_heal_retries_once(monkeypatch=None):
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    m = MarzbanMigrator(
        MigrationJob(job_id="start-heal"),
        {"target_db": "sqlite"},
    )
    calls = {"n": 0}

    async def fake_start(migrator, *, health_max_wait=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection refused")
        return None

    async def run():
        with (
            patch(
                "app.services.migrators.marzban.safe_start_pasarguard",
                new=fake_start,
            ),
            patch.object(m, "_stop_panel", new=AsyncMock()),
            patch.object(m, "_ensure_target_database_stack", new=AsyncMock()),
            patch.object(m, "_try_sync_db_auth", new=AsyncMock()),
        ):
            await m._safe_start_with_heal()
        assert calls["n"] == 2

    asyncio.run(run())
    print("OK: panel start auto-heal retries once")


if __name__ == "__main__":
    test_merge_copy_tree_and_safe_replace_on_collision()
    test_normalize_templates_rename_and_merge()
    test_transient_infra_classification()
    test_hard_fail_asserts_still_raise()
    test_safe_start_with_heal_retries_once()
    print("All marzban migrate heal tests passed")
