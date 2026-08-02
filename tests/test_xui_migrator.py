"""Tests for 3x-ui DB resolution (bundle workspace vs file vs zip)."""

import asyncio
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.migrators.xui import (
    find_xui_db_in_dir,
    resolve_xui_db_source,
    XuiMigrator,
)
from app.services.migrators.base import MigrationJob
from app.services.upload_bundle import init_bundle, save_bundle_slot, prepare_bundle_workspace
from app.services.upload_requirements import get_upload_requirements


def test_find_xui_db_prefers_named_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "other.db").write_bytes(b"other")
        nested = root / "etc" / "x-ui"
        nested.mkdir(parents=True)
        target = nested / "x-ui.db"
        target.write_bytes(b"xui")
        found = find_xui_db_in_dir(root)
        assert found == target


def test_find_xui_db_fallback_any_db():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        only = root / "db.sqlite3"
        only.write_bytes(b"sqlite")
        assert find_xui_db_in_dir(root) == only


def test_resolve_workspace_directory_not_treated_as_file():
    """Regression: upload_path = bundle workspace dir must not IsADirectoryError."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "bundles" / "fc73394a-62a" / "workspace"
        workspace.mkdir(parents=True)
        db = workspace / "x-ui.db"
        db.write_bytes(b"xui-data")

        resolved = resolve_xui_db_source(str(workspace), str(workspace))
        assert resolved == db
        assert resolved.is_file()


def test_resolve_upload_work_dir_nested_zip_layout():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "workspace"
        nested = work / "opt" / "x-ui"
        nested.mkdir(parents=True)
        db = nested / "x-ui.db"
        db.write_bytes(b"nested")

        resolved = resolve_xui_db_source(str(work), str(work))
        assert resolved == db


def test_resolve_single_db_file():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "x-ui.db"
        db.write_bytes(b"file")
        assert resolve_xui_db_source(str(db)) == db


def test_resolve_falls_back_to_live_install():
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "live-x-ui.db"
        live.write_bytes(b"live")
        with patch("app.services.migrators.xui.find_xui_db", return_value=live):
            assert resolve_xui_db_source(None, None) == live


def test_resolve_missing_returns_none():
    with patch("app.services.migrators.xui.find_xui_db", return_value=None):
        assert resolve_xui_db_source("/nonexistent/path", None) is None


def test_locate_xui_db_from_workspace_async():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            db = workspace / "x-ui.db"
            db.write_bytes(b"xui")
            job = MigrationJob(job_id="testxui1")
            migrator = XuiMigrator(job, {})
            found = await migrator._locate_xui_db(str(workspace), str(workspace))
            assert found == db

    asyncio.run(_run())


def test_locate_xui_db_from_zip_async():
    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "backup.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("etc/x-ui/x-ui.db", b"zip-xui")

            job = MigrationJob(job_id="testxui2")
            migrator = XuiMigrator(job, {})
            with patch("app.services.migrators.xui.BACKUP_DIR", Path(tmp) / "backups"):
                found = await migrator._locate_xui_db(str(zip_path), None)
            assert found is not None
            assert found.name == "x-ui.db"
            assert found.read_bytes() == b"zip-xui"

    asyncio.run(_run())


def test_bundle_database_slot_preserves_xui_db_name():
    bid = init_bundle()
    result = save_bundle_slot(
        bid, "database", b"xui-sqlite", "x-ui.db",
        panel_id="3x-ui", source_db="sqlite",
    )
    assert result["ok"] is True
    assert result["bundle_status"]["complete"] is True
    work = prepare_bundle_workspace(bid)
    assert (work / "x-ui.db").exists()
    assert (work / "x-ui.db").read_bytes() == b"xui-sqlite"

    # Same path the API passes into the migrator
    resolved = resolve_xui_db_source(str(work), str(work))
    assert resolved == work / "x-ui.db"


def test_bundle_zip_slot_xui():
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "xui.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("x-ui.db", b"from-zip")

        bid = init_bundle()
        result = save_bundle_slot(
            bid, "bundle_zip", zip_path.read_bytes(), "xui.zip",
            panel_id="3x-ui", source_db="sqlite",
        )
        assert result["ok"] is True
        assert result["bundle_status"]["complete"] is True
        work = prepare_bundle_workspace(bid)
        found = find_xui_db_in_dir(work)
        assert found is not None
        assert found.read_bytes() == b"from-zip"


def test_xui_upload_requirements_always_have_slots():
    with patch("app.services.upload_requirements.find_xui_db", return_value=None):
        reqs = get_upload_requirements("3x-ui", "sqlite")
        assert reqs["upload_mode"] == "required"
        ids = [s["id"] for s in reqs["slots"]]
        assert "bundle_zip" in ids
        assert "database" in ids

    with patch("app.services.upload_requirements.find_xui_db", return_value=Path("/etc/x-ui/x-ui.db")):
        reqs = get_upload_requirements("3x-ui", "sqlite")
        assert reqs["upload_mode"] == "optional"
        ids = [s["id"] for s in reqs["slots"]]
        assert "database" in ids


def test_run_does_not_copy2_directory():
    """End-to-end guard: run() must fail past DB locate without IsADirectoryError."""
    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "x-ui.db").write_bytes(b"xui")

            job = MigrationJob(job_id="testxui3")
            migrator = XuiMigrator(job, {
                "upload_path": str(workspace),
                "upload_work_dir": str(workspace),
                "target_db": "sqlite",
            })

            # PasarGuard not installed → should raise clear RuntimeError after locate
            with patch("app.services.migrators.xui.PASARGUARD_DIR", Path(tmp) / "missing-pg"):
                with patch("app.services.migrators.xui.BACKUP_DIR", Path(tmp) / "backups"):
                    try:
                        await migrator.run({
                            "upload_path": str(workspace),
                            "upload_work_dir": str(workspace),
                            "target_db": "sqlite",
                        })
                        raise AssertionError("expected RuntimeError for missing PasarGuard")
                    except RuntimeError as e:
                        assert "PasarGuard" in str(e)
                        assert "IsADirectory" not in str(e)

            # Confirm input was staged as a file copy, not attempted on the directory
            assert any("Using 3x-ui database" in line for line in job.logs)

    asyncio.run(_run())


if __name__ == "__main__":
    test_find_xui_db_prefers_named_file()
    test_find_xui_db_fallback_any_db()
    test_resolve_workspace_directory_not_treated_as_file()
    test_resolve_upload_work_dir_nested_zip_layout()
    test_resolve_single_db_file()
    test_resolve_falls_back_to_live_install()
    test_resolve_missing_returns_none()
    test_locate_xui_db_from_workspace_async()
    test_locate_xui_db_from_zip_async()
    test_bundle_database_slot_preserves_xui_db_name()
    test_bundle_zip_slot_xui()
    test_xui_upload_requirements_always_have_slots()
    test_run_does_not_copy2_directory()
    print("\nAll x-ui migrator tests passed.")
