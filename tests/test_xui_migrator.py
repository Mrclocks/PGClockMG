"""Tests for 3x-ui DB resolution (bundle workspace vs file vs zip)."""

import asyncio
import json
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
    assert_migrated_core_config,
    patch_xui_converter_tag_bug,
    normalize_subscription_mapping,
    _subscription_path_only,
    build_redirect_server_config,
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


def _make_pg_sqlite(path: Path, users: int = 1, inbounds: int = 1, core_configs: int = 1) -> Path:
    conn = sqlite3.connect(path)
    for table in ("users", "admins", "hosts", "inbounds", "nodes", "groups", "core_configs"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    for i in range(users):
        conn.execute("INSERT INTO users VALUES (?)", (i + 1,))
    for i in range(inbounds):
        conn.execute("INSERT INTO inbounds VALUES (?)", (i + 1,))
    for i in range(core_configs):
        conn.execute("INSERT INTO core_configs VALUES (?)", (i + 1,))
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


def test_normalize_subscription_mapping_strips_query():
    """Clients hit /sub/{subId}; upstream mapping used /sub/{id}?name={id} → 404."""
    with tempfile.TemporaryDirectory() as tmp:
        mapping = Path(tmp) / "m.json"
        mapping.write_text(
            json.dumps({
                "mappings": {
                    "u1": {
                        "old_subscription_url": "/sub/abc123?name=abc123",
                        "new_subscription_url": "/sub/newtoken",
                    },
                    "u2": {
                        "old_subscription_url": "https://x.example:2096/sub/zz?name=zz",
                        "new_subscription_url": "sub/other",
                    },
                }
            }),
            encoding="utf-8",
        )
        normalize_subscription_mapping(mapping)
        data = json.loads(mapping.read_text(encoding="utf-8"))
        assert data["mappings"]["u1"]["old_subscription_url"] == "/sub/abc123"
        assert data["mappings"]["u2"]["old_subscription_url"] == "/sub/zz"
        assert data["mappings"]["u2"]["new_subscription_url"] == "/sub/other"
        assert _subscription_path_only("/sub/a?name=a") == "/sub/a"


def test_build_redirect_config_sets_domain_and_port():
    cfg = build_redirect_server_config(
        listen_port=2096,
        redirect_domain="https://1.2.3.4:8000",
    )
    assert cfg["port"] == 2096
    assert cfg["redirect_domain"] == "https://1.2.3.4:8000"
    assert cfg["ssl"]["enabled"] is False


def test_install_redirect_uses_native_pg_redirect():
    """Wizard installs bundled pg-redirect (no GitHub binary download)."""
    async def _run():
        job = MigrationJob(job_id="redir1")
        migrator = XuiMigrator(job, {})
        seen = []

        async def _fake_cmd(self, cmd, cwd=None, timeout=600):
            seen.append(cmd)
            return True, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "subscription_url_mapping.json"
            mapping.write_text(
                json.dumps({
                    "mappings": {
                        "a": {
                            "old_subscription_url": "/sub/tok?name=tok",
                            "new_subscription_url": "/sub/new",
                        }
                    }
                }),
                encoding="utf-8",
            )
            tools = Path(tmp) / "tools"
            pkg = tools / "pg_redirect"
            # Minimal stub package so bundled_pg_redirect_src finds something;
            # install script is mocked via _run_cmd.
            real = Path(__file__).resolve().parents[1] / "tools" / "pg_redirect"
            import shutil
            shutil.copytree(real, pkg)

            with patch("app.services.redirect_ops.TOOLS_DIR", tools), \
                 patch("app.services.redirect_ops.BASE_DIR", Path(tmp)), \
                 patch.object(XuiMigrator, "_run_cmd", _fake_cmd):
                ok, err = await migrator._install_redirect_server(
                    mapping,
                    listen_port=2096,
                    redirect_domain="http://10.0.0.1:8000",
                )
            assert ok and not err
            blob = " ".join(
                c if isinstance(c, str) else " ".join(c) for c in seen
            )
            assert "pg-redirect" in blob
            assert "/opt/pg-redirect" in blob
            assert "github.com/PasarGuard/migrations/releases" not in blob
            data = json.loads(mapping.read_text(encoding="utf-8"))
            assert data["mappings"]["a"]["old_subscription_url"] == "/sub/tok"
            cfg = json.loads(
                (mapping.parent / "pg-redirect-config.json").read_text(encoding="utf-8")
            )
            assert cfg["port"] == 2096
            assert cfg["redirect_base"] == "http://10.0.0.1:8000"

    asyncio.run(_run())


def test_install_redirect_http_when_xui_had_no_sub_tls():
    """Old http://sub links: plain HTTP even if PasarGuard URL is https://."""
    async def _run():
        job = MigrationJob(job_id="redir2")
        migrator = XuiMigrator(job, {})
        installs = []

        async def _fake_install(migrator, mapping, **kwargs):
            installs.append(kwargs)
            return True, ""

        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "subscription_url_mapping.json"
            mapping.write_text(
                json.dumps({
                    "mappings": {
                        "a": {
                            "old_subscription_url": "/sub/tok",
                            "new_subscription_url": "/sub/new",
                        }
                    }
                }),
                encoding="utf-8",
            )
            with patch(
                "app.services.redirect_ops.install_pg_redirect", _fake_install,
            ):
                ok, err = await migrator._install_redirect_server(
                    mapping,
                    listen_port=2096,
                    redirect_domain="https://10.0.0.1:8000",
                    ssl_cert="/root/cert/ip/fullchain.pem",
                    ssl_key="/root/cert/ip/privkey.pem",
                    ssl_wanted=False,
                )
            assert ok and not err
            assert len(installs) == 1
            assert not installs[0].get("ssl_cert")
            assert not installs[0].get("ssl_key")

    asyncio.run(_run())


def test_install_redirect_retries_self_signed_when_https_required():
    """When old links need HTTPS, never silent-HTTP; retry self-signed instead."""
    async def _run():
        job = MigrationJob(job_id="redir3")
        migrator = XuiMigrator(job, {})
        installs = []

        async def _fake_install(migrator, mapping, **kwargs):
            installs.append(kwargs)
            if len(installs) == 1:
                return False, "ssl boom"
            assert kwargs.get("ssl_cert") == "SELF_CERT"
            return True, ""

        async def _fake_active(_migrator):
            return False

        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "subscription_url_mapping.json"
            mapping.write_text(
                json.dumps({
                    "mappings": {
                        "a": {
                            "old_subscription_url": "/sub/tok",
                            "new_subscription_url": "/sub/new",
                        }
                    }
                }),
                encoding="utf-8",
            )
            with patch(
                "app.services.redirect_ops.install_pg_redirect", _fake_install,
            ), patch(
                "app.services.redirect_ops.pg_redirect_is_active", _fake_active,
            ), patch(
                "app.services.redirect_ops.resolve_redirect_tls",
                return_value=("", "", ""),
            ), patch(
                "app.services.redirect_ops.generate_self_signed_pem",
                return_value=("SELF_CERT", "SELF_KEY"),
            ):
                ok, err = await migrator._install_redirect_server(
                    mapping,
                    listen_port=2096,
                    redirect_domain="https://10.0.0.1:8000",
                    ssl_wanted=True,
                    work_dir=Path(tmp),
                )
            assert ok and not err
            assert len(installs) == 2
            assert installs[1]["ssl_cert"] == "SELF_CERT"

    asyncio.run(_run())


def test_resolve_redirect_tls_prefers_pasarguard_over_xui():
    from app.services.redirect_ops import resolve_redirect_tls

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pg_cert = root / "pg.crt"
        pg_key = root / "pg.key"
        xui_cert = root / "xui.crt"
        xui_key = root / "xui.key"
        pg_cert.write_text("-----BEGIN CERTIFICATE-----\nPG\n-----END CERTIFICATE-----\n")
        pg_key.write_text("-----BEGIN PRIVATE KEY-----\nPGK\n-----END PRIVATE KEY-----\n")
        xui_cert.write_text("-----BEGIN CERTIFICATE-----\nXUI\n-----END CERTIFICATE-----\n")
        xui_key.write_text("-----BEGIN PRIVATE KEY-----\nXUIK\n-----END PRIVATE KEY-----\n")
        env = (
            f"UVICORN_SSL_CERTFILE={pg_cert}\n"
            f"UVICORN_SSL_KEYFILE={pg_key}\n"
        )
        cert, key, src = resolve_redirect_tls(
            cert_path=str(xui_cert),
            key_path=str(xui_key),
            env_text=env,
            want_ssl=True,
            work_dir=root,
        )
        assert src == "pasarguard-uvicorn-ssl"
        assert "PG" in cert
        assert "PGK" in key


def test_build_runtime_config_accepts_embedded_pem():
    """PasarGuard may store fullchain PEM in UVICORN_SSL_* — must not Path().is_file() it."""
    from app.services.redirect_ops import build_runtime_config

    # Long-ish PEM blob (Errno 36 repro when treated as filename)
    cert = "-----BEGIN CERTIFICATE-----\n" + ("A" * 4000) + "\n-----END CERTIFICATE-----\n"
    key = "-----BEGIN PRIVATE KEY-----\n" + ("B" * 2000) + "\n-----END PRIVATE KEY-----\n"
    cfg = build_runtime_config(
        listen_port=2096,
        redirect_base="https://10.0.0.1:8000",
        ssl_cert=cert,
        ssl_key=key,
    )
    assert cfg["ssl"]["enabled"] is True
    assert cfg["ssl"]["cert"].startswith("-----BEGIN CERTIFICATE-----")
    assert "AAAA" in cfg["ssl"]["cert"]


def test_normalize_target_db_aliases():
    from app.services.pasarguard_ops import normalize_target_db, mysql_client_bins

    assert normalize_target_db("postgres") == "postgresql"
    assert normalize_target_db("timescale") == "timescaledb"
    assert normalize_target_db("maria") == "mariadb"
    assert normalize_target_db("MySQL") == "mysql"
    assert mysql_client_bins("mariadb", "mariadb")[0] == "mariadb"
    assert mysql_client_bins("mysql", "mysql")[0] == "mysql"


def test_convert_landed_sqlite_syncs_mysql_and_finalizes_env():
    """Regression: panel Access denied pasarguard after copy-as-root."""
    asyncio.run(_assert_convert_for_target("mysql"))


def test_convert_landed_sqlite_for_all_server_engines():
    """sqlite→mysql/mariadb/postgresql/timescaledb all finalize .env + sync roles."""
    for engine in ("mysql", "mariadb", "postgresql", "timescaledb"):
        asyncio.run(_assert_convert_for_target(engine))


async def _assert_convert_for_target(target_db: str):
    with tempfile.TemporaryDirectory() as tmp:
        pg_dir = Path(tmp) / "opt" / "pasarguard"
        data = Path(tmp) / "var" / "lib" / "pasarguard"
        pg_dir.mkdir(parents=True)
        data.mkdir(parents=True)
        land = data / "db.sqlite3"
        land.write_bytes(b"x")

        if target_db in ("mysql", "mariadb"):
            install_env = (
                f"SQLALCHEMY_DATABASE_URL="
                f'"mysql+asyncmy://pasarguard:appsecret@127.0.0.1:3306/pasarguard"\n'
                "DB_USER=pasarguard\n"
                "DB_PASSWORD=appsecret\n"
                "DB_NAME=pasarguard\n"
                "MYSQL_ROOT_PASSWORD=rootsecret\n"
            )
            url_needle = "mysql+asyncmy://"
            secret = "rootsecret"
            port = "3306"
            admin_user = "root"
        else:
            install_env = (
                f"SQLALCHEMY_DATABASE_URL="
                f'"postgresql+asyncpg://pasarguard:pgsecret@127.0.0.1:5432/pasarguard"\n'
                "DB_USER=pasarguard\n"
                "DB_PASSWORD=pgsecret\n"
                "DB_NAME=pasarguard\n"
                "POSTGRES_USER=postgres\n"
                "POSTGRES_PASSWORD=pgsecret\n"
            )
            url_needle = "postgresql+asyncpg://"
            secret = "pgsecret"
            port = "5432"
            admin_user = "postgres"

        (pg_dir / ".env").write_text(install_env, encoding="utf-8")

        job = MigrationJob(job_id=f"conv-{target_db}")
        migrator = XuiMigrator(job, {"target_db": target_db})
        calls = {"mysql_sync": 0, "pg_sync": 0, "cross": 0, "cross_tgt": None}

        async def _fake_cross(migrator, path, src, tgt):
            calls["cross"] += 1
            calls["cross_tgt"] = tgt
            migrator.params = {
                "target_db": tgt,
                "_resolved_target_conn": {
                    "user": admin_user,
                    "password": secret,
                    "database": "pasarguard",
                    "host": "127.0.0.1",
                    "port": port,
                    "db_type": tgt,
                },
            }

        async def _fake_mysql_sync(migrator, db_type, admin, **kw):
            calls["mysql_sync"] += 1
            assert db_type == target_db
            assert kw.get("app_user") == "pasarguard"
            assert kw.get("password") == secret

        async def _fake_pg_sync(migrator, db_type, admin, env_text=None):
            calls["pg_sync"] += 1
            assert db_type == target_db

        async def _fake_resolve(migrator, db_type, env_text=None):
            return {
                "user": admin_user,
                "password": secret,
                "database": "pasarguard",
                "db_type": db_type,
            }

        with patch("app.services.migrators.xui.PASARGUARD_DIR", pg_dir), \
             patch("app.services.migrators.xui.PASARGUARD_DATA", data), \
             patch("app.services.migrators.xui.PASARGUARD_ENV", pg_dir / ".env"), \
             patch("app.services.migrators.xui.run_cross_db_migration", _fake_cross), \
             patch(
                 "app.services.db_auth.resolve_live_admin_connection",
                 _fake_resolve,
             ), \
             patch(
                 "app.services.db_auth.sync_mysql_roles_to_password",
                 _fake_mysql_sync,
             ), \
             patch(
                 "app.services.db_auth.sync_postgres_roles_to_app_password",
                 _fake_pg_sync,
             ):
            await migrator._convert_landed_sqlite_to_target(
                land, target_db, install_env,
            )

        assert calls["cross"] == 1
        assert calls["cross_tgt"] == target_db
        if target_db in ("mysql", "mariadb"):
            assert calls["mysql_sync"] == 1
            assert calls["pg_sync"] == 0
        else:
            assert calls["pg_sync"] == 1
            assert calls["mysql_sync"] == 0
        env_now = (pg_dir / ".env").read_text(encoding="utf-8")
        assert url_needle in env_now
        assert "pasarguard" in env_now
        assert secret in env_now
        assert not land.exists()
        assert list(data.glob("db.sqlite3.pre-convert-*.bak"))


def test_patch_xui_converter_tag_bug_moves_assignment():
    """Upstream uses `tag` in debug logs before assignment when externalProxy exists."""
    buggy = '''
            # Remove external proxy and TLS settings from streamSettings if present
            if isinstance(stream_settings, dict):
                # Remove proxy-related settings
                stream_settings.pop("proxySettings", None)
                stream_settings.pop("sockopt", None)
                # Remove external proxy
                if "externalProxy" in stream_settings:
                    stream_settings.pop("externalProxy")
                    logger.debug(f"Removed externalProxy from inbound {tag}")
                # Remove TLS settings (certificate files won't exist on new system)
                if "tlsSettings" in stream_settings:
                    stream_settings.pop("tlsSettings")
                    logger.debug(f"Removed tlsSettings from inbound {tag}")
                # Remove security field (TLS indicator)
                if "security" in stream_settings:
                    stream_settings.pop("security")
                    logger.debug(f"Removed security field from inbound {tag}")
            
            tag = inbound_row.get("tag", f"inbound-{inbound_row.get('id', 'unknown')}")
            protocol = inbound_row.get("protocol", "vless")
'''
    with tempfile.TemporaryDirectory() as tmp:
        tool = Path(tmp) / "x-ui"
        conv = tool / "migration" / "transformers" / "converter.py"
        conv.parent.mkdir(parents=True)
        conv.write_text("prefix\n" + buggy + "\nsuffix\n", encoding="utf-8")
        assert patch_xui_converter_tag_bug(tool) is True
        text = conv.read_text(encoding="utf-8")
        assign = text.find(
            'tag = inbound_row.get("tag", f"inbound-{inbound_row.get(\'id\', \'unknown\')}")'
        )
        use = text.find('Removed externalProxy from inbound {tag}')
        assert assign != -1 and use != -1
        assert assign < use
        # idempotent
        assert patch_xui_converter_tag_bug(tool) is False


def test_assert_migrated_core_config_rejects_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "db.sqlite3"
        _make_pg_sqlite(path, users=1, inbounds=1, core_configs=0)
        try:
            assert_migrated_core_config(path)
            raise AssertionError("expected empty core_configs error")
        except RuntimeError as e:
            assert "core_configs" in str(e)


def test_run_cmd_shell_string_uses_subprocess_shell():
    """Regression: db_auth MySQL probe passes shell strings; must not char-split exec."""
    async def _run():
        job = MigrationJob(job_id="shell1")
        migrator = XuiMigrator(job, {})

        class FakeProc:
            returncode = 0
            stdout = None

            def __init__(self):
                self._done = False

            async def wait(self):
                return 0

            def kill(self):
                pass

        calls = {"shell": 0, "exec": 0}

        async def fake_shell(cmd, **kwargs):
            calls["shell"] += 1
            assert isinstance(cmd, str)
            assert "docker compose exec" in cmd
            proc = FakeProc()

            class Out:
                async def readline(self):
                    return b""

            proc.stdout = Out()
            return proc

        async def fake_exec(*args, **kwargs):
            calls["exec"] += 1
            raise AssertionError("shell string must not use create_subprocess_exec")

        with patch("asyncio.create_subprocess_shell", fake_shell), \
             patch("asyncio.create_subprocess_exec", fake_exec):
            ok, _ = await migrator._run_cmd(
                'cd "/opt/pasarguard" && docker compose exec -T mysql mysql -u root -p"x" -N -e "SELECT 1"',
                timeout=5,
            )
        assert ok
        assert calls["shell"] == 1
        assert calls["exec"] == 0
        # Must not log character-spaced command
        assert not any("$ c d" in line for line in job.logs)

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
    test_normalize_subscription_mapping_strips_query()
    test_build_redirect_config_sets_domain_and_port()
    test_install_redirect_uses_native_pg_redirect()
    test_install_redirect_http_when_xui_had_no_sub_tls()
    test_install_redirect_retries_self_signed_when_https_required()
    test_resolve_redirect_tls_prefers_pasarguard_over_xui()
    test_build_runtime_config_accepts_embedded_pem()
    test_normalize_target_db_aliases()
    test_convert_landed_sqlite_syncs_mysql_and_finalizes_env()
    test_convert_landed_sqlite_for_all_server_engines()
    test_patch_xui_converter_tag_bug_moves_assignment()
    test_assert_migrated_core_config_rejects_empty()
    test_run_cmd_shell_string_uses_subprocess_shell()
    print("\nAll x-ui migrator tests passed.")
