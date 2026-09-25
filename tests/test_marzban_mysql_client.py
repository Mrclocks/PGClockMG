"""Marzban → PasarGuard MySQL/MariaDB client selection (no hardcoded mysql)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.migrators.base import MigrationJob
from app.services.migrators.marzban import MarzbanMigrator
from app.services.pasarguard_ops import mysql_client_bins


def _migrator(target_db: str = "mariadb") -> MarzbanMigrator:
    job = MigrationJob(job_id="maria-client")
    return MarzbanMigrator(job, {"target_db": target_db})


def test_mysql_client_bins_prefers_mariadb_for_mariadb_target():
    assert mysql_client_bins("mariadb", "mariadb") == ["mariadb", "mysql"]
    assert mysql_client_bins("mysql", "mysql") == ["mysql", "mariadb"]
    print("OK: client bin order")


def test_resolve_pasarguard_mysql_service_prefers_mariadb():
    m = _migrator("mariadb")

    async def _run():
        with patch(
            "app.services.migrators.marzban.resolve_db_service",
            side_effect=lambda name: "mariadb" if name == "mariadb" else None,
        ):
            svc, engine = await m._resolve_pasarguard_mysql_service()
        assert engine == "mariadb"
        assert svc == "mariadb"

    asyncio.run(_run())
    print("OK: resolve prefers mariadb service for mariadb target")


def test_pick_mysql_client_skips_missing_mysql_uses_mariadb():
    """User bug: MariaDB image has no `mysql` → exit 127; must use `mariadb`."""
    m = _migrator("mariadb")
    calls: list[str] = []

    class _Proc:
        def __init__(self, code: int, out: bytes):
            self.returncode = code
            self._out = out

        async def communicate(self):
            return self._out, None

    async def fake_exec(*argv, **_kwargs):
        idx = list(argv).index("-u")
        bin_name = argv[idx - 1]
        calls.append(bin_name)
        if bin_name == "mysql":
            return _Proc(
                127,
                b'OCI runtime exec failed: exec: "mysql": '
                b"executable file not found in $PATH\n",
            )
        if bin_name == "mariadb":
            return _Proc(0, b"1\n")
        return _Proc(1, b"unexpected")

    async def _run():
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            chosen = await m._pick_mysql_client_bin(
                "mariadb", "root", "secret", "127.0.0.1", "mariadb",
            )
        assert chosen == "mariadb"
        # mariadb preferred first for mariadb target — never needs the missing mysql
        assert calls[0] == "mariadb"
        assert "mysql" not in calls

    asyncio.run(_run())
    print("OK: pick uses mariadb first on mariadb target")


def test_pick_mysql_client_falls_back_when_preferred_missing():
    """If preferred client is missing from the image, try the alternate."""
    m = _migrator("mysql")
    calls: list[str] = []

    class _Proc:
        def __init__(self, code: int, out: bytes):
            self.returncode = code
            self._out = out

        async def communicate(self):
            return self._out, None

    async def fake_exec(*argv, **_kwargs):
        idx = list(argv).index("-u")
        bin_name = argv[idx - 1]
        calls.append(bin_name)
        if bin_name == "mysql":
            return _Proc(
                127,
                b'OCI runtime exec failed: exec: "mysql": '
                b"executable file not found in $PATH\n",
            )
        return _Proc(0, b"1\n")

    async def _run():
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            chosen = await m._pick_mysql_client_bin(
                "mysql", "root", "secret", "127.0.0.1", "mysql",
            )
        assert chosen == "mariadb"
        assert calls == ["mysql", "mariadb"]

    asyncio.run(_run())
    print("OK: pick falls back when preferred client missing")


def test_pick_mysql_client_tries_mysql_then_mariadb_on_mysql_image():
    m = _migrator("mysql")
    calls: list[str] = []

    class _Proc:
        def __init__(self, code: int, out: bytes):
            self.returncode = code
            self._out = out

        async def communicate(self):
            return self._out, None

    async def fake_exec(*argv, **_kwargs):
        idx = list(argv).index("-u")
        bin_name = argv[idx - 1]
        calls.append(bin_name)
        if bin_name == "mysql":
            return _Proc(0, b"1\n")
        return _Proc(127, b'exec: "mariadb": executable file not found\n')

    async def _run():
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            chosen = await m._pick_mysql_client_bin(
                "mysql", "root", "secret", "127.0.0.1", "mysql",
            )
        assert chosen == "mysql"
        assert calls[0] == "mysql"

    asyncio.run(_run())
    print("OK: mysql target uses mysql client first")


def test_import_recreate_uses_picked_client_not_hardcoded_mysql(tmp_path, monkeypatch):
    """Recreate DB must not shell out to hardcoded `mysql` (MariaDB exit 127)."""
    import app.services.migrators.marzban as marz_mod

    dump = tmp_path / "marzban.sql"
    dump.write_text("CREATE TABLE users (id INT);\n", encoding="utf-8")
    fixed = tmp_path / "fixed_import.sql"

    m = _migrator("mariadb")
    m.params.update({
        "target_db_user": "root",
        "target_db_password": "secret",
        "target_db_name": "pasarguard",
        "target_db_host": "127.0.0.1",
        "_auto_db_credentials": True,
        "_resolved_target_conn": {
            "user": "root",
            "password": "secret",
            "database": "pasarguard",
            "host": "127.0.0.1",
            "port": "3306",
            "db_type": "mariadb",
        },
    })

    wipe_bins: list[str] = []
    import_bins: list[str] = []

    class _WipeProc:
        returncode = 0

        async def communicate(self):
            return b"", None

    class _ImportProc:
        returncode = 0
        stdout = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def fake_exec(*argv, **kwargs):
        argv = list(argv)
        if "-e" in argv and any(
            isinstance(a, str) and "CREATE DATABASE" in a for a in argv
        ):
            idx = argv.index("-u")
            wipe_bins.append(argv[idx - 1])
            return _WipeProc()
        # stdin import: has db name as last arg, no -e SQL
        if kwargs.get("stdin") is not None:
            idx = argv.index("-u")
            import_bins.append(argv[idx - 1])
            return _ImportProc()
        return _WipeProc()

    async def _run():
        with patch.object(m, "_run_cmd", new_callable=AsyncMock, return_value=(True, "")), \
             patch.object(m, "_wait_compose_mysql_ready", new_callable=AsyncMock), \
             patch.object(m, "_pick_mysql_client_bin", new_callable=AsyncMock, return_value="mariadb"), \
             patch.object(m, "_resolve_pasarguard_mysql_service", new_callable=AsyncMock, return_value=("mariadb", "mariadb")), \
             patch(
                 "app.services.migrators.marzban.rewrite_mysql_dump_file_for_pasarguard",
                 return_value=0,
             ), \
             patch(
                 "app.services.mysql_import_diagnostics.assess_mysql_import_ram",
                 return_value=MagicMock(level="ok", message="ram ok"),
             ), \
             patch(
                 "app.services.mysql_import_diagnostics.write_mysql_import_stdin_file",
                 side_effect=lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes()),
             ), \
             patch(
                 "app.services.mysql_import_diagnostics.compose_service_running",
                 new_callable=AsyncMock,
                 return_value=True,
             ), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch.object(marz_mod, "PASARGUARD_DIR", tmp_path):
            # Make rewrite write fixed next to dump
            def _rewrite(src, dst):
                Path(dst).write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
                return 1

            with patch(
                "app.services.migrators.marzban.rewrite_mysql_dump_file_for_pasarguard",
                side_effect=_rewrite,
            ):
                await m._import_mysql_dump(dump)

        assert wipe_bins == ["mariadb"], wipe_bins
        assert import_bins == ["mariadb"], import_bins
        assert all(b != "mysql" for b in wipe_bins + import_bins)

    asyncio.run(_run())
    print("OK: import recreate/import use picked mariadb client")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
