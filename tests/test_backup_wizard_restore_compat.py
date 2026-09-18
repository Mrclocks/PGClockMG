"""End-to-end: create PGClockBackup full-bundles for every DB engine and prove
the wizard restore analyzer + artifact discovery accept them without blockers.

No live Docker DB is required — SQL engines use realistic dump payloads that
mirror what mysqldump/pg_dump produce; sqlite uses a real online backup copy.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.services.backup_engine import (
    _sql_dump_looks_complete,
    create_backup_bundle,
    resolve_backup_path,
    verify_backup_archive,
)
from app.services.migrators.base import MigrationJob
from app.services.pg_restore import (
    _find_env,
    _restore_data_files,
    analyze_pasarguard_backup,
    discover_backup_artifacts,
)


ENGINES = (
    "sqlite",
    "mysql",
    "mariadb",
    "postgresql",
    "timescaledb",
)

# Soft-family pairs the wizard auto-continues (no convert_blocked).
SOFT_INSTALL_TARGETS = {
    "mysql": ("mysql", "mariadb"),
    "mariadb": ("mariadb", "mysql"),
    "postgresql": ("postgresql", "timescaledb"),
    "timescaledb": ("timescaledb",),  # timescale→plain PG is not soft
    "sqlite": ("sqlite",),
}

REQUIRED_ZIP_MEMBERS = (
    ".env",
    "pgclockmg-manifest.json",
    "docker-compose.yml",
    "certs/fullchain.pem",
    "templates/user.html",
    "xray_config.json",
)


def _sqlalchemy_url(db_type: str) -> str:
    if db_type == "sqlite":
        return "sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"
    if db_type in ("mysql", "mariadb"):
        return "mysql+pymysql://pasarguard:app-secret@127.0.0.1:3306/pasarguard"
    return "postgresql+asyncpg://pasarguard:app-secret@127.0.0.1:5432/pasarguard"


def _realistic_pg_dump(*, users: int = 5) -> str:
    rows = "\n".join(f"{i}\tuser{i}" for i in range(1, users + 1))
    return (
        "--\n-- PostgreSQL database dump\n--\n"
        "SET statement_timeout = 0;\n"
        "SET client_encoding = 'UTF8';\n"
        "CREATE TABLE public.users (id integer, username text);\n"
        "CREATE TABLE public.admins (id integer);\n"
        "CREATE TABLE public.nodes (id integer);\n"
        "CREATE TABLE public.inbounds (id integer);\n"
        "CREATE TABLE public.hosts (id integer);\n"
        'CREATE TABLE public."groups" (id integer);\n'
        "COPY public.users (id, username) FROM stdin;\n"
        f"{rows}\n"
        "\\.\n"
        "COPY public.admins (id) FROM stdin;\n"
        "1\n"
        "\\.\n"
        "COPY public.nodes (id) FROM stdin;\n"
        "1\n"
        "2\n"
        "\\.\n"
        "COPY public.inbounds (id) FROM stdin;\n"
        "1\n"
        "\\.\n"
    )


def _realistic_mysql_dump(*, users: int = 5) -> str:
    values = ",".join(f"({i},'user{i}')" for i in range(1, users + 1))
    return (
        "-- MySQL dump 10.13  Distrib 8.0.36\n"
        "-- Host: localhost    Database: pasarguard\n"
        "/*!40101 SET NAMES utf8mb4 */;\n"
        "CREATE TABLE `users` (`id` int, `username` varchar(64));\n"
        f"INSERT INTO `users` VALUES {values};\n"
        "CREATE TABLE `admins` (`id` int);\n"
        "INSERT INTO `admins` VALUES (1);\n"
        "CREATE TABLE `nodes` (`id` int);\n"
        "INSERT INTO `nodes` VALUES (1),(2);\n"
        "CREATE TABLE `inbounds` (`id` int);\n"
        "INSERT INTO `inbounds` VALUES (1);\n"
        "CREATE TABLE `hosts` (`id` int);\n"
        "CREATE TABLE `groups` (`id` int);\n"
    )


def _prep_panel_tree(root: Path, db_type: str) -> tuple[Path, Path]:
    pg_dir = root / "opt" / "pasarguard"
    pg_data = root / "var" / "lib" / "pasarguard"
    pg_dir.mkdir(parents=True)
    pg_data.mkdir(parents=True)

    env_lines = [
        f'SQLALCHEMY_DATABASE_URL="{_sqlalchemy_url(db_type)}"',
        'APP_VERSION="1.2.3"',
        "DB_USER=pasarguard",
        "DB_PASSWORD=app-secret",
        "DB_NAME=pasarguard",
    ]
    if db_type in ("mysql", "mariadb"):
        env_lines += [
            "MYSQL_USER=pasarguard",
            "MYSQL_PASSWORD=app-secret",
            "MYSQL_DATABASE=pasarguard",
            "MYSQL_ROOT_PASSWORD=root-secret",
        ]
    elif db_type in ("postgresql", "timescaledb"):
        env_lines += [
            "POSTGRES_USER=pasarguard",
            "POSTGRES_PASSWORD=app-secret",
            "POSTGRES_DB=pasarguard",
        ]
    (pg_dir / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    (pg_dir / "docker-compose.yml").write_text(
        "services:\n  pasarguard:\n    image: x\n"
        f"  {db_type if db_type != 'sqlite' else 'pasarguard'}:\n    image: db\n",
        encoding="utf-8",
    )

    if db_type == "sqlite":
        db = pg_data / "db.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        conn.executemany("INSERT INTO users(username) VALUES (?)", [("a",), ("b",), ("c",), ("d",), ("e",)])
        for table in ("admins", "nodes", "inbounds", "hosts", "groups"):
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO admins(id) VALUES (1)")
        conn.execute("INSERT INTO nodes(id) VALUES (1), (2)")
        conn.execute("INSERT INTO inbounds(id) VALUES (1)")
        conn.commit()
        conn.close()

    (pg_data / "certs").mkdir()
    (pg_data / "certs" / "fullchain.pem").write_text("CERT-FULLCHAIN", encoding="utf-8")
    (pg_data / "certs" / "privkey.pem").write_text("CERT-KEY", encoding="utf-8")
    (pg_data / "xray_config.json").write_text('{"inbounds":[]}', encoding="utf-8")
    (pg_data / "templates").mkdir()
    (pg_data / "templates" / "user.html").write_text("<html>user</html>", encoding="utf-8")
    return pg_dir, pg_data


def _wire_backup_paths(monkeypatch, eng, root: Path, pg_dir: Path, pg_data: Path, db_type: str) -> None:
    monkeypatch.setattr(eng, "PASARGUARD_DIR", pg_dir)
    monkeypatch.setattr(eng, "PASARGUARD_ENV", pg_dir / ".env")
    monkeypatch.setattr(eng, "PASARGUARD_DATA", pg_data)
    monkeypatch.setattr(eng, "BACKUP_DIR", root / "backups")
    monkeypatch.setattr(eng, "WORK_DIR", root / "work")
    monkeypatch.setattr(eng, "BACKUP_JOBS_DIR", root / "jobs")
    (root / "backups").mkdir(parents=True, exist_ok=True)
    (root / "work").mkdir(parents=True, exist_ok=True)
    (root / "jobs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(eng, "is_pasarguard_installed", lambda: True)
    monkeypatch.setattr(eng, "get_pasarguard_db_type", lambda: db_type)
    monkeypatch.setattr(
        eng,
        "live_panel_stats",
        lambda: {
            "counts": {
                "users": 5,
                "nodes": 2,
                "admins": 1,
                "inbounds": 1,
                "hosts": 0,
                "groups": 0,
            }
        },
    )
    import app.services.backup_settings as settings

    monkeypatch.setattr(settings, "BACKUP_SETTINGS_FILE", root / "settings.json")


def _install_fake_dumpers(monkeypatch, eng, db_type: str) -> None:
    if db_type == "sqlite":
        return

    def _fake_pg(_dt, dest: Path, job: dict) -> None:
        dest.write_text(_realistic_pg_dump(users=5), encoding="utf-8")
        (dest.parent / "globals.sql").write_text(
            "--\n-- PostgreSQL database cluster dump\n--\n"
            "CREATE ROLE pasarguard WITH LOGIN PASSWORD 'app-secret';\n",
            encoding="utf-8",
        )
        job.setdefault("logs", []).append(f"fake pg_dump {_dt}")

    def _fake_mysql(_dt, dest: Path, job: dict) -> None:
        dest.write_text(_realistic_mysql_dump(users=5), encoding="utf-8")
        job.setdefault("logs", []).append(f"fake mysqldump {_dt}")

    if db_type in ("postgresql", "timescaledb"):
        monkeypatch.setattr(eng, "_dump_postgres", _fake_pg)
    else:
        monkeypatch.setattr(eng, "_dump_mysql", _fake_mysql)


def _build_bundle(tmp_path: Path, monkeypatch, db_type: str) -> tuple[Path, dict]:
    import app.services.backup_engine as eng

    root = tmp_path / db_type
    root.mkdir(parents=True, exist_ok=True)
    pg_dir, pg_data = _prep_panel_tree(root, db_type)
    _wire_backup_paths(monkeypatch, eng, root, pg_dir, pg_data, db_type)
    _install_fake_dumpers(monkeypatch, eng, db_type)

    job = create_backup_bundle(trigger="wizard-compat-test")
    assert job["status"] == "success", (db_type, job.get("error"), job.get("logs"))
    path = resolve_backup_path(job["backup_id"])
    assert path and path.is_file(), job
    return path, job


@pytest.mark.parametrize("db_type", ENGINES)
def test_backup_bundle_wizard_analyze_same_engine(tmp_path, monkeypatch, db_type):
    """Create → verify zip → wizard analyze must be ok for same-engine restore."""
    import app.services.pg_restore as restore_mod

    zip_path, job = _build_bundle(tmp_path, monkeypatch, db_type)

    verified = verify_backup_archive(zip_path)
    assert verified.get("ok") is True, verified

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
    for member in REQUIRED_ZIP_MEMBERS:
        assert member in names, f"{db_type}: missing {member} in {sorted(names)}"
    if db_type == "sqlite":
        assert "db.sqlite3" in names
    else:
        assert "db_backup.sql" in names
    if db_type in ("postgresql", "timescaledb"):
        assert "globals.sql" in names

    manifest = job.get("manifest") or {}
    assert manifest.get("format") == "pgclockmg-full-bundle"
    assert manifest.get("db_type") == db_type
    assert manifest.get("counts", {}).get("users") == 5

    monkeypatch.setattr(restore_mod, "is_pasarguard_installed", lambda: True)
    monkeypatch.setattr(restore_mod, "get_pasarguard_db_type", lambda: db_type)
    monkeypatch.setattr(restore_mod, "WORK_DIR", tmp_path / db_type / "analyze-work")
    (tmp_path / db_type / "analyze-work").mkdir(parents=True, exist_ok=True)

    analysis = analyze_pasarguard_backup(path=zip_path)
    assert analysis["ok"] is True, (db_type, analysis.get("warnings"))
    assert analysis["backup_db"] == db_type
    assert analysis["installed_db"] == db_type
    assert analysis["db_match"] is True
    assert analysis["convert_blocked"] is False
    assert analysis["experimental_db_change"] is False
    assert analysis["has_env"] is True
    assert analysis["layout"] in ("single", "sqlite_file")
    assert analysis["dump_name"] in ("db_backup.sql", "db.sqlite3")
    assert (analysis.get("table_counts") or {}).get("users", 0) >= 5


@pytest.mark.parametrize("db_type,installed", [
    ("mysql", "mariadb"),
    ("mariadb", "mysql"),
    ("postgresql", "timescaledb"),
])
def test_backup_bundle_wizard_analyze_soft_family(tmp_path, monkeypatch, db_type, installed):
    """Soft-family installs must stay ok (wizard continues without convert block)."""
    import app.services.pg_restore as restore_mod

    zip_path, _job = _build_bundle(tmp_path, monkeypatch, db_type)
    monkeypatch.setattr(restore_mod, "is_pasarguard_installed", lambda: True)
    monkeypatch.setattr(restore_mod, "get_pasarguard_db_type", lambda: installed)
    monkeypatch.setattr(restore_mod, "WORK_DIR", tmp_path / f"{db_type}-soft" / "work")
    (tmp_path / f"{db_type}-soft" / "work").mkdir(parents=True, exist_ok=True)

    analysis = analyze_pasarguard_backup(path=zip_path)
    assert analysis["ok"] is True, analysis.get("warnings")
    assert analysis["soft_match"] is True
    assert analysis["convert_blocked"] is False
    assert analysis["experimental_db_change"] is False


def test_all_engines_restore_artifacts_and_data_files(tmp_path, monkeypatch):
    """Extracted bundles must expose dump+env and restore certs/templates/xray paths."""
    import app.services.pg_restore as restore_mod

    for db_type in ENGINES:
        zip_path, _job = _build_bundle(tmp_path / "build", monkeypatch, db_type)
        extract = tmp_path / "extract" / db_type
        extract.mkdir(parents=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract)

        env_path = _find_env(extract)
        assert env_path is not None and env_path.is_file()
        env_text = env_path.read_text(encoding="utf-8")
        assert f"PASARGUARD_DB_ENGINE={db_type}" in env_text

        art = discover_backup_artifacts(extract, env_db=db_type)
        if db_type == "sqlite":
            assert art["layout"] == "sqlite_file"
            assert art.get("sqlite_path")
            assert Path(art["sqlite_path"]).is_file()
            assert Path(art["sqlite_path"]).stat().st_size >= 64
        else:
            assert art["layout"] == "single"
            dump = Path(art["dump_path"])
            assert dump.is_file()
            assert _sql_dump_looks_complete(dump)
            assert dump.name == "db_backup.sql"

        # Wizard data-file restore path (certs / templates / xray)
        dest_data = tmp_path / "restore-dest" / db_type / "data"
        dest_opt = tmp_path / "restore-dest" / db_type / "opt"
        dest_data.mkdir(parents=True)
        dest_opt.mkdir(parents=True)
        monkeypatch.setattr(restore_mod, "PASARGUARD_DATA", dest_data)
        monkeypatch.setattr(restore_mod, "PASARGUARD_DIR", dest_opt)

        job = MigrationJob(job_id=f"compat-{db_type}")
        asyncio.run(_restore_data_files(job, extract))

        assert (dest_data / "certs" / "fullchain.pem").is_file()
        assert (dest_data / "certs" / "privkey.pem").is_file()
        assert (dest_data / "templates" / "user.html").is_file()
        assert (dest_data / "xray_config.json").is_file()
        assert "CERT-FULLCHAIN" in (dest_data / "certs" / "fullchain.pem").read_text(encoding="utf-8")


def test_matrix_covers_every_engine_and_soft_target():
    """Guardrail: keep this suite aligned with SOFT_INSTALL_TARGETS / ENGINES."""
    assert set(ENGINES) == set(SOFT_INSTALL_TARGETS)
    for eng, targets in SOFT_INSTALL_TARGETS.items():
        assert eng in targets
        assert eng in ENGINES
