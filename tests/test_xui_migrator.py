"""Tests for 3x-ui DB resolution (bundle workspace vs file vs zip)."""

import asyncio
import sqlite3
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
    resolve_xui_schema_db,
    bundled_xui_schema_db,
    assert_xui_source_has_data,
    assert_migrated_pg_has_data,
    XuiMigrator,
)
from app.services.migrators.base import MigrationJob
from app.services.upload_bundle import init_bundle, save_bundle_slot, prepare_bundle_workspace
from app.services.upload_requirements import get_upload_requirements


def _make_xui_db(path: Path, clients: int = 1, inbounds: int = 1) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY, remark TEXT)")
    conn.execute(
        "CREATE TABLE client_traffics (id INTEGER PRIMARY KEY, email TEXT, inbound_id INTEGER)"
    )
    for i in range(inbounds):
        conn.execute("INSERT INTO inbounds VALUES (?, ?)", (i + 1, f"in{i}"))
    for i in range(clients):
        conn.execute(
            "INSERT INTO client_traffics VALUES (?, ?, ?)",
            (i + 1, f"u{i}@t.com", 1),
        )
    conn.commit()
    conn.close()
    return path


def _make_pg_sqlite(path: Path, users: int = 1, inbounds: int = 1) -> Path:
    conn = sqlite3.connect(path)
    for table in ("users", "admins", "hosts", "inbounds", "nodes", "groups"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    for i in range(users):
        conn.execute("INSERT INTO users VALUES (?)", (i + 1,))
    for i in range(inbounds):
        conn.execute("INSERT INTO inbounds VALUES (?)", (i + 1,))
    if users or inbounds:
        conn.execute("INSERT INTO groups VALUES (1)")
    conn.commit()
    conn.close()
    return path


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


def test_schema_prefers_bundled_over_missing_live_sqlite():
    """MySQL PasarGuard installs have no db.sqlite3 — use bundled schema."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = Path(tmp) / "tools"
        bundled = tools / "migrations" / "x-ui" / "input-db-pg" / "db.sqlite3"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"schema")
        data = Path(tmp) / "pasarguard-data"  # empty — simulates MySQL install
        data.mkdir()

        resolved = resolve_xui_schema_db(tools_dir=tools, pasarguard_data=data)
        assert resolved == bundled
        assert bundled_xui_schema_db(tools) == bundled


def test_schema_falls_back_to_live_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        tools = Path(tmp) / "tools"
        tools.mkdir()
        data = Path(tmp) / "data"
        data.mkdir()
        live = data / "db.sqlite3"
        live.write_bytes(b"live-schema")

        resolved = resolve_xui_schema_db(tools_dir=tools, pasarguard_data=data)
        assert resolved == live


def test_schema_missing_raises_clear_error():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            resolve_xui_schema_db(
                tools_dir=Path(tmp) / "tools",
                pasarguard_data=Path(tmp) / "data",
            )
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "schema reference" in str(e).lower() or "input-db-pg" in str(e)


def test_assert_xui_source_has_data():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_xui_db(Path(tmp) / "x-ui.db", clients=3, inbounds=2)
        counts = assert_xui_source_has_data(db)
        assert counts["client_traffics"] == 3
        assert counts["inbounds"] == 2

        empty = Path(tmp) / "empty.db"
        conn = sqlite3.connect(empty)
        conn.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE client_traffics (id INTEGER PRIMARY KEY, email TEXT)")
        conn.commit()
        conn.close()
        try:
            assert_xui_source_has_data(empty)
            raise AssertionError("expected empty source error")
        except RuntimeError as e:
            assert "خالی" in str(e)


def test_assert_migrated_pg_has_data():
    with tempfile.TemporaryDirectory() as tmp:
        ok = _make_pg_sqlite(Path(tmp) / "ok.sqlite3", users=2, inbounds=1)
        counts = assert_migrated_pg_has_data(ok)
        assert counts["users"] == 2

        empty = _make_pg_sqlite(Path(tmp) / "empty.sqlite3", users=0, inbounds=0)
        try:
            assert_migrated_pg_has_data(empty)
            raise AssertionError("expected empty output error")
        except RuntimeError as e:
            assert "خالی" in str(e) or "0" in str(e)


def test_run_does_not_copy2_directory():
    """End-to-end guard: run() must fail past DB locate without IsADirectoryError."""
    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            _make_xui_db(workspace / "x-ui.db")

            job = MigrationJob(job_id="testxui3")
            migrator = XuiMigrator(job, {
                "upload_path": str(workspace),
                "upload_work_dir": str(workspace),
                "target_db": "sqlite",
            })

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

            assert any("Using 3x-ui database" in line for line in job.logs)
            assert any("Source x-ui counts" in line for line in job.logs)

    asyncio.run(_run())


def test_run_uses_bundled_schema_not_mysql_start():
    """Regression: MySQL PG install must not start panel just to invent db.sqlite3."""
    async def _run():
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            _make_xui_db(workspace / "x-ui.db", clients=2, inbounds=1)

            tools = Path(tmp) / "tools"
            schema = tools / "migrations" / "x-ui" / "input-db-pg" / "db.sqlite3"
            schema.parent.mkdir(parents=True)
            # minimal schema file for path resolution (migrate itself is mocked)
            schema.write_bytes(b"x")
            (tools / "migrations" / "x-ui").mkdir(parents=True, exist_ok=True)

            pg_dir = Path(tmp) / "opt" / "pasarguard"
            pg_dir.mkdir(parents=True)
            data = Path(tmp) / "var" / "lib" / "pasarguard"
            data.mkdir(parents=True)  # no db.sqlite3 → MySQL-style install

            job = MigrationJob(job_id="testxui4")
            migrator = XuiMigrator(job, {
                "upload_path": str(workspace),
                "upload_work_dir": str(workspace),
                "target_db": "mysql",
            })

            started = {"n": 0}

            async def _fake_start(_self):
                started["n"] += 1

            async def _fake_cmd(self, cmd, cwd=None, timeout=600):
                if cmd and cmd[0] == "uv" and "migrate.py" in cmd:
                    out_folder = Path(cmd[cmd.index("--output-folder") + 1])
                    out_folder.mkdir(parents=True, exist_ok=True)
                    _make_pg_sqlite(out_folder / "db.sqlite3", users=2, inbounds=1)
                    return True, "ok"
                return True, "ok"

            async def _fake_cross_db(*_a, **_k):
                raise RuntimeError("cross-db-called")

            with patch("app.services.migrators.xui.PASARGUARD_DIR", pg_dir), \
                 patch("app.services.migrators.xui.PASARGUARD_DATA", data), \
                 patch("app.services.migrators.xui.PASARGUARD_ENV", pg_dir / ".env"), \
                 patch("app.services.migrators.xui.TOOLS_DIR", tools), \
                 patch("app.services.migrators.xui.BACKUP_DIR", Path(tmp) / "backups"), \
                 patch.object(XuiMigrator, "_run_cmd", _fake_cmd), \
                 patch("app.services.migrators.xui.safe_start_pasarguard", _fake_start), \
                 patch("app.services.migrators.xui.run_cross_db_migration", _fake_cross_db):
                try:
                    await migrator.run({
                        "upload_path": str(workspace),
                        "upload_work_dir": str(workspace),
                        "target_db": "mysql",
                        "install_redirect": False,
                    })
                    raise AssertionError("expected cross-db-called")
                except RuntimeError as e:
                    assert "cross-db-called" in str(e)

            # Must use bundled schema; must NOT start PG before migrate for schema inventing
            assert started["n"] == 0
            assert any("input-db-pg" in line for line in job.logs)
            assert any("Migrated SQLite counts" in line for line in job.logs)
            assert (data / "db.sqlite3").exists()

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
    test_schema_prefers_bundled_over_missing_live_sqlite()
    test_schema_falls_back_to_live_sqlite()
    test_schema_missing_raises_clear_error()
    test_assert_xui_source_has_data()
    test_assert_migrated_pg_has_data()
    test_run_does_not_copy2_directory()
    test_run_uses_bundled_schema_not_mysql_start()
    print("\nAll x-ui migrator tests passed.")
