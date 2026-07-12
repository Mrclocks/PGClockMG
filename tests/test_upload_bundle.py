"""Tests for upload bundle slots."""

import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.upload_bundle import init_bundle, save_bundle_slot, validate_bundle, prepare_bundle_workspace
from app.services.upload_requirements import get_upload_requirements


def test_marzban_zip_bundle():
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "backup.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("var/lib/marzban/db.sqlite3", "sqlite")
            zf.writestr("var/lib/marzban/.env", 'SQLALCHEMY_DATABASE_URL = "sqlite:////var/lib/marzban/db.sqlite3"\n')

        bid = init_bundle()
        result = save_bundle_slot(
            bid, "bundle_zip", zip_path.read_bytes(), "backup.zip",
            panel_id="marzban", source_db="sqlite", marzban_mode="fresh",
        )
        assert result["bundle_status"]["complete"] is True
        work = prepare_bundle_workspace(bid)
        assert (work / "db.sqlite3").exists() or list(work.rglob("db.sqlite3"))
        print("OK: marzban zip bundle")


def test_marzban_separate_slots():
    bid = init_bundle()
    r1 = save_bundle_slot(
        bid, "database", b"sqlite-content", "db.sqlite3",
        panel_id="marzban", source_db="sqlite", marzban_mode="fresh",
    )
    assert r1["ok"] is True
    assert r1["bundle_status"]["complete"] is True

    r2 = save_bundle_slot(
        bid, "env", b"SQLALCHEMY_DATABASE_URL=sqlite\n", ".env",
        panel_id="marzban", source_db="sqlite", marzban_mode="fresh",
    )
    work = prepare_bundle_workspace(bid)
    assert (work / "db.sqlite3").exists()
    assert (work / ".env").exists()
    print("OK: marzban separate slots")


def test_requirements_fresh_marzban():
    reqs = get_upload_requirements("marzban", "sqlite", "fresh")
    assert reqs["upload_mode"] == "required"
    ids = [s["id"] for s in reqs["slots"]]
    assert "bundle_zip" in ids
    assert "database" in ids
    print("OK: requirements")


if __name__ == "__main__":
    test_requirements_fresh_marzban()
    test_marzban_zip_bundle()
    test_marzban_separate_slots()
    print("\nAll upload bundle tests passed.")
