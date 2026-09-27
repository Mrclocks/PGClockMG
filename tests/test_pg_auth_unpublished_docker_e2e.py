"""E2E: reproduce production sqlite→Timescale SASL / trust hard-fail.

Real docker Postgres with:
  - official local+127.0.0.1 trust (password probes via exec are worthless)
  - NO published 5432 (the exact gate that killed v4.6.13 resolves)
  - volume SCRAM secret ≠ install .env password

Proves v4.6.14 resolve_live_admin_connection:
  1. does NOT raise “published host port/image could not be resolved”
  2. force-aligns roles to install password
  3. proves password via in-container eth0 SCRAM
  4. returns a host:port the migrator can use (bridge IP)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INSTALL_PASSWORD = "install-secret-v4614"
STALE_PASSWORD = "stale-volume-scram"
PG_USER = "pasarguard"
PG_DB = "pasarguard"
SERVICE = "timescaledb"
COMPOSE_NAME = "pgclockmg-auth-e2e"


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run(cmd: list[str], *, cwd: str | None = None, env: dict | None = None, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(
        cmd,
        cwd=cwd,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode, out


@pytest.fixture(scope="module")
def unpublished_pg(tmp_path_factory):
    if not _docker_ok():
        pytest.skip("docker daemon not available")

    root = tmp_path_factory.mktemp("pg-auth-e2e")
    compose = root / "docker-compose.yml"
    # Intentionally NO ports: — mirrors production PgBouncer-only / unpublished DB.
    compose.write_text(
        textwrap.dedent(
            f"""
            name: {COMPOSE_NAME}
            services:
              {SERVICE}:
                image: postgres:16-alpine
                environment:
                  POSTGRES_USER: {PG_USER}
                  POSTGRES_PASSWORD: {STALE_PASSWORD}
                  POSTGRES_DB: {PG_DB}
                # no ports:
                healthcheck:
                  test: ["CMD-SHELL", "pg_isready -U {PG_USER} -d {PG_DB}"]
                  interval: 1s
                  timeout: 3s
                  retries: 30
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    _run(["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"], cwd=str(root), timeout=60)
    code, out = _run(
        ["docker", "compose", "-f", str(compose), "up", "-d"],
        cwd=str(root),
        timeout=180,
    )
    assert code == 0, f"compose up failed: {out}"

    # Wait healthy
    for _ in range(60):
        code, out = _run(
            ["docker", "compose", "-f", str(compose), "ps", "--format", "{{.Status}}"],
            cwd=str(root),
            timeout=30,
        )
        if "healthy" in (out or "").lower() or "(healthy)" in (out or "").lower():
            break
        # alpine health can lag; also accept running + ready
        code2, out2 = _run(
            [
                "docker", "compose", "-f", str(compose), "exec", "-T",
                SERVICE, "pg_isready", "-U", PG_USER, "-d", PG_DB,
            ],
            cwd=str(root),
            timeout=20,
        )
        if code2 == 0:
            break
        time.sleep(1)
    else:
        _run(["docker", "compose", "-f", str(compose), "logs"], cwd=str(root), timeout=30)
        pytest.fail("postgres container did not become ready")

    # Confirm: local trust accepts bogus password; TCP with install password fails;
    # no published 5432.
    code, out = _run(
        [
            "docker", "compose", "-f", str(compose), "exec", "-T",
            "-e", "PGPASSWORD=definitely-wrong",
            SERVICE, "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", "SELECT 1",
        ],
        cwd=str(root),
        timeout=30,
    )
    assert code == 0 and "1" in out, f"expected local trust, got: {out}"

    # Confirm unpublished: Ports JSON has null / empty HostPort bindings.
    code, cid = _run(
        ["docker", "compose", "-f", str(compose), "ps", "-q", SERVICE],
        cwd=str(root),
        timeout=20,
    )
    assert code == 0 and cid.strip(), cid
    code, ports_json = _run(
        [
            "docker", "inspect", "--format",
            "{{json .NetworkSettings.Ports}}", cid.strip().splitlines()[0],
        ],
        timeout=20,
    )
    assert code == 0, ports_json
    assert '"HostPort"' not in (ports_json or ""), (
        f"expected no published HostPort, got: {ports_json}"
    )
    # compose port may print ":0" quirk — ignore; inspect is authoritative.

    yield {"root": root, "compose": compose}

    _run(
        ["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
        cwd=str(root),
        timeout=90,
    )


def test_resolve_survives_trust_and_unpublished_5432(unpublished_pg):
    import asyncio
    from unittest.mock import patch

    from app.services.db_auth import resolve_live_admin_connection
    from app.services.migrators.base import BaseMigrator, MigrationJob

    root = unpublished_pg["root"]
    compose = unpublished_pg["compose"]
    env_text = (
        f"POSTGRES_PASSWORD={INSTALL_PASSWORD}\n"
        f"POSTGRES_USER={PG_USER}\n"
        f"POSTGRES_DB={PG_DB}\n"
        f"DB_PASSWORD={INSTALL_PASSWORD}\n"
        f"DB_USER={PG_USER}\n"
        f"DB_NAME={PG_DB}\n"
        "PASARGUARD_DB_ENGINE=timescaledb\n"
    )

    class Dummy(BaseMigrator):
        async def run(self, params):
            return {}

    job = MigrationJob(job_id="e2e-unpublished")
    migrator = Dummy(job, {})

    async def run_cmd(cmd, cwd=None, timeout=600, *, quiet=False):
        # Rewrite plain `docker compose …` to pin our compose file + project dir.
        argv = list(cmd) if isinstance(cmd, list) else [cmd]
        if len(argv) >= 2 and argv[0] == "docker" and argv[1] == "compose":
            argv = [
                "docker", "compose",
                "-f", str(compose),
                *argv[2:],
            ]
            cwd = str(root)
        code, out = await asyncio.to_thread(_run, argv, cwd=cwd, timeout=int(timeout or 120))
        return code == 0, out

    async def _go():
        with patch("app.services.db_auth.PASARGUARD_DIR", root), \
             patch("app.services.db_auth.resolve_db_service", return_value=SERVICE), \
             patch("app.services.multiworker_stack.compose_has_service", return_value=False), \
             patch.object(migrator, "_run_cmd", run_cmd):
            return await resolve_live_admin_connection(
                migrator, "timescaledb", env_text=env_text,
            )

    conn = asyncio.run(_go())

    assert conn["password"] == INSTALL_PASSWORD, conn
    assert conn["user"] == PG_USER
    assert conn["database"] == PG_DB
    assert conn["port"] == "5432"
    # Must be bridge IP (no 127.0.0.1 publish exists)
    assert conn["host"] and conn["host"] != "127.0.0.1", conn
    assert not any("refusing to accept" in line.lower() for line in job.logs)
    assert any("scram" in line.lower() or "force-align" in line.lower() for line in job.logs)

    # Prove returned endpoint actually accepts install password over TCP from host.
    code, out = _run(
        [
            "docker", "run", "--rm", "--network", "host",
            "-e", f"PGPASSWORD={INSTALL_PASSWORD}",
            "--entrypoint", "psql",
            "postgres:16-alpine",
            "-h", conn["host"], "-p", conn["port"],
            "-U", PG_USER, "-d", PG_DB, "-tAc", "SELECT 1",
        ],
        timeout=90,
    )
    assert code == 0 and "1" in out, (
        f"host TCP to {conn['host']}:{conn['port']} failed after heal: {out}\n"
        f"logs:\n" + "\n".join(job.logs[-40:])
    )

    # Stale password must NOT work anymore on eth0/host TCP.
    code2, out2 = _run(
        [
            "docker", "run", "--rm", "--network", "host",
            "-e", f"PGPASSWORD={STALE_PASSWORD}",
            "--entrypoint", "psql",
            "postgres:16-alpine",
            "-h", conn["host"], "-p", conn["port"],
            "-U", PG_USER, "-d", PG_DB, "-tAc", "SELECT 1",
        ],
        timeout=90,
    )
    assert code2 != 0, f"stale password still accepted after heal: {out2}"

    print("E2E OK: unpublished 5432 + trust → eth0 SCRAM heal → install password works")
    print(f"  endpoint={conn['host']}:{conn['port']} user={conn['user']}")
    for line in job.logs:
        if any(k in line.lower() for k in ("scram", "force-align", "heal", "trust", "endpoint")):
            print(f"  log: {line}")


def test_fixture_is_trust_and_unpublished(unpublished_pg):
    """Sanity: container still has local trust and no HostPort publish."""
    root = unpublished_pg["root"]
    compose = unpublished_pg["compose"]

    code, out = _run(
        [
            "docker", "compose", "-f", str(compose), "exec", "-T",
            "-e", "PGPASSWORD=wrong",
            SERVICE, "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", "SELECT 1",
        ],
        cwd=str(root),
        timeout=30,
    )
    assert code == 0 and "1" in out, out

    code, cid = _run(
        ["docker", "compose", "-f", str(compose), "ps", "-q", SERVICE],
        cwd=str(root),
        timeout=20,
    )
    code, ports_json = _run(
        [
            "docker", "inspect", "--format",
            "{{json .NetworkSettings.Ports}}", cid.strip().splitlines()[0],
        ],
        timeout=20,
    )
    assert '"HostPort"' not in (ports_json or ""), ports_json
    print("E2E fixture OK: local trust + unpublished 5432")
