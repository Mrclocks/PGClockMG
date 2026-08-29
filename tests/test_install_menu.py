"""Menu, status panel and uninstall/redirect actions in install.sh.

install.sh is sourced with PG_MIGRATOR_INSTALL_LIB=1 (helpers only, no install)
and every path it inspects is redirected into a sandbox, so the real
/opt/pasarguard, /etc/pg-redirect and systemd units are never touched. A pty
stands in for the operator wherever the script asks a question.
"""

from __future__ import annotations

import json
import os
import pty
import re
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Sandbox:
    """Fake server layout: PasarGuard, systemd units, wizard/backup install, redirect."""

    def __init__(self, tmp: str):
        self.root = Path(tmp)
        self.pasarguard = self.root / "pasarguard"
        self.systemd = self.root / "systemd"
        self.install = self.root / "install"
        self.backup = self.root / "backup"
        self.redirect = self.root / "redirect"
        self.keep = self.root / "keep"
        for path in (self.pasarguard, self.systemd, self.install, self.redirect):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def env(self) -> dict:
        base = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("PG_MIGRATOR_") and not k.startswith("PG_BACKUP_")
        }
        return {
            **base,
            "PG_MIGRATOR_INSTALL_LIB": "1",
            "PG_MIGRATOR_INSTALL_DIR": str(self.install),
            "PG_BACKUP_INSTALL_DIR": str(self.backup),
            "PG_MIGRATOR_SYSTEMD_DIR": str(self.systemd),
            "PG_MIGRATOR_PASARGUARD_DIR": str(self.pasarguard),
            "PG_MIGRATOR_REDIRECT_DIR": str(self.redirect),
            "PG_MIGRATOR_BACKUP_DIR": str(self.keep),
        }

    def write_pasarguard(self, env_text: str, compose: str | None = None) -> None:
        (self.pasarguard / ".env").write_text(env_text, encoding="utf-8")
        if compose is not None:
            (self.pasarguard / "docker-compose.yml").write_text(compose, encoding="utf-8")

    def write_wizard(self, version: str = "9.9.9", port: int = 8443) -> None:
        (self.install / "app").mkdir(parents=True, exist_ok=True)
        (self.install / "app" / "main.py").write_text(
            f'APP_VERSION = "{version}"\n', encoding="utf-8"
        )
        (self.systemd / "pg-migrator.service").write_text(
            "[Service]\n"
            f"WorkingDirectory={self.install}\n"
            "ExecStart=/opt/pg-migrator/venv/bin/python -m uvicorn app.main:app "
            f"--host 0.0.0.0 --port {port}\n",
            encoding="utf-8",
        )

    def write_backup(self, version: str = "9.9.9", port: int = 7001, workdir: Path | None = None) -> None:
        root = workdir or self.backup
        root.mkdir(parents=True, exist_ok=True)
        (root / "app").mkdir(parents=True, exist_ok=True)
        (root / "app" / "backup_main.py").write_text(
            f'APP_VERSION = "{version}"\n', encoding="utf-8"
        )
        (root / "backup_panel").mkdir(parents=True, exist_ok=True)
        (root / "backups").mkdir(parents=True, exist_ok=True)
        (self.systemd / "pg-backup.service").write_text(
            "[Service]\n"
            f"WorkingDirectory={root}\n"
            f"Environment=PG_BACKUP_HOME={root}\n"
            "ExecStart=/opt/pg-backup/venv/bin/python -m uvicorn app.backup_main:app "
            f"--host 0.0.0.0 --port {port}\n",
            encoding="utf-8",
        )

    def write_redirect(self, **cfg) -> None:
        config = {
            "host": "0.0.0.0",
            "port": 2096,
            "extra_ports": [],
            "redirect_base": "https://panel.example.com:8000",
            "panel": "x-ui",
            "ssl": {"enabled": False},
        }
        config.update(cfg)
        (self.redirect / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (self.redirect / "mapping.json").write_text("{}", encoding="utf-8")
        (self.systemd / "pg-redirect.service").write_text("[Service]\n", encoding="utf-8")

    def run(self, snippet: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", f'source "{INSTALL_SH}"\n{snippet}'],
            capture_output=True,
            text=True,
            env={**self.env, **(extra_env or {})},
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # no controlling terminal -> non-interactive path
            timeout=120,
        )

    def run_on_tty(
        self,
        snippet: str,
        keystrokes: list[str],
        extra_env: dict | None = None,
        settle: float = 1.0,
    ) -> tuple[int, str]:
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["bash", "-c", f'source "{INSTALL_SH}"\n{snippet}'],
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**self.env, **(extra_env or {})},
            )
            for keys in keystrokes:
                os.write(master, keys.encode())
                time.sleep(settle)
            out, _ = proc.communicate(timeout=180)
            return proc.returncode, ANSI.sub("", out)
        finally:
            os.close(master)
            os.close(slave)


def plain(text: str) -> str:
    return ANSI.sub("", text)


def sandbox() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory()


# ── status panel ──────────────────────────────────────────────────────────────


def test_pasarguard_database_detection():
    cases = [
        ('SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3"', None, "sqlite"),
        (
            'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://u:p@db:5432/pg"',
            "services:\n  timescaledb:\n    image: timescale/timescaledb-ha:pg17\n",
            "timescaledb",
        ),
        (
            'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://u:p@db:5432/pg"',
            "services:\n  db:\n    image: postgres:16\n",
            "postgresql",
        ),
        (
            'SQLALCHEMY_DATABASE_URL="mysql+asyncmy://u:p@db/pg"',
            "services:\n  db:\n    image: mariadb:11\n",
            "mariadb",
        ),
        (
            'PASARGUARD_DB_ENGINE=timescaledb\nSQLALCHEMY_DATABASE_URL="sqlite:////x.db"',
            None,
            "timescaledb",
        ),
    ]
    with sandbox() as tmp:
        box = Sandbox(tmp)
        for env_text, compose, expected in cases:
            (box.pasarguard / "docker-compose.yml").unlink(missing_ok=True)
            box.write_pasarguard(env_text + "\n", compose)
            res = box.run("pasarguard_db_type; echo")
            assert res.stdout.strip() == expected, f"{env_text} -> {res.stdout!r}"
    print("OK: PasarGuard database engine detection")


def test_pasarguard_version_from_compose():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_pasarguard(
            'SQLALCHEMY_DATABASE_URL="sqlite:////x.db"\n',
            "services:\n  pasarguard:\n    image: ghcr.io/pasarguard/panel:v1.4.2\n",
        )
        res = box.run("pasarguard_version; echo")
        assert res.stdout.strip() == "v1.4.2", res.stdout

        # An untagged image carries no version — say "unknown", never a raw yaml line
        (box.pasarguard / "docker-compose.yml").write_text(
            "services:\n  pasarguard:\n    image: pasarguard/panel\n", encoding="utf-8"
        )
        res = box.run("pasarguard_version; echo")
        assert res.stdout.strip() == "unknown", res.stdout

        (box.pasarguard / "docker-compose.yml").unlink()
        res = box.run("pasarguard_version; echo")
        assert res.stdout.strip() == "unknown", res.stdout
    print("OK: PasarGuard version detection")


def test_status_panel_reports_installed_stack():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_pasarguard(
            'SQLALCHEMY_DATABASE_URL="postgresql+asyncpg://u:p@db/pg"\n',
            "services:\n  pasarguard:\n    image: ghcr.io/pasarguard/panel:v1.4.2\n"
            "  timescaledb:\n    image: timescale/timescaledb-ha:pg17\n",
        )
        box.write_wizard(version="3.2.8", port=8443)
        box.write_redirect(port=443, extra_ports=[2083], panel="hiddify", ssl={"enabled": True})

        out = plain(box.run("print_status").stdout)
        assert "PasarGuard" in out and "v1.4.2" in out
        assert "timescaledb" in out
        assert "3.2.8" in out and "8443" in out
        assert "hiddify" in out and "443, 2083" in out
        assert "Installer" in out
        assert "was not found" not in out
    print("OK: status panel lists panel, database, wizard and redirect")


def test_status_panel_warns_when_pasarguard_missing():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        out = plain(box.run("print_status").stdout)
        assert "[MISS]" in out
        assert "PasarGuard was not found" in out
        assert "docs.pasarguard.org" in out
        assert "not installed" in out
    print("OK: missing PasarGuard is reported in English with an install hint")


def test_menu_lists_the_six_entries():
    with sandbox() as tmp:
        out = plain(Sandbox(tmp).run("print_menu").stdout)
        for entry in (
            "1)  Install / update PGClockMG",
            "2)  Install / update PGClockBackup",
            "3)  Uninstall PGClockMG",
            "4)  Uninstall PGClockBackup",
            "5)  Redirect server",
            "6)  Exit",
        ):
            assert entry in out, out
    print("OK: main menu shows install / uninstall for wizard + backup / redirect / exit")


# ── install action ────────────────────────────────────────────────────────────


def test_ensure_access_token_writes_0600_file_once():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        out1 = plain(box.run("ensure_access_token").stdout + box.run("ensure_access_token").stdout)
        path = box.install / ".access_token"
        assert path.is_file(), out1
        token = path.read_text(encoding="utf-8").strip()
        assert re.fullmatch(r"[0-9a-f]{48}", token), token
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)
        # Second call must keep the same token (updates must not rotate it).
        box.run("ensure_access_token")
        assert path.read_text(encoding="utf-8").strip() == token
    print("OK: ensure_access_token creates a stable 0600 token file")


def test_ensure_access_token_soft_fails_without_killing_the_script():
    """Menu status used to die here because ensure_access_token called exit."""
    with sandbox() as tmp:
        box = Sandbox(tmp)
        # Replace the install directory with a file so mkdir -p cannot succeed.
        if box.install.exists():
            if box.install.is_dir():
                import shutil
                shutil.rmtree(box.install)
            else:
                box.install.unlink()
        box.install.write_text("not-a-directory", encoding="utf-8")
        # set -e would abort on a non-zero return — the point is that ensure
        # itself must not call exit, so the caller can handle the failure.
        res = box.run(
            "set +e\n"
            "ensure_access_token\n"
            "echo RC=$?\n"
            "echo SURVIVED"
        )
        out = plain(res.stdout + res.stderr)
        assert res.returncode == 0, out
        assert "SURVIVED" in out, out
        assert "RC=1" in out, out
        assert "Cannot create" in out or "Cannot write" in out or "Failed to generate" in out
    print("OK: ensure_access_token returns soft failure instead of exiting")


def test_print_success_survives_missing_token():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_wizard(version="3.2.8", port=7000)
        res = box.run(
            "ensure_access_token() { return 1; }\n"
            "read_access_token() { return 1; }\n"
            "print_success; echo SURVIVED"
        )
        out = plain(res.stdout + res.stderr)
        assert res.returncode == 0, out
        assert "SURVIVED" in out, out
        assert "PGClockMG is installed and running" in out, out
        assert "Access token was not created" in out, out
    print("OK: print_success stays alive when token creation fails")


def test_install_asks_the_port_then_installs_and_prints_the_url():
    """run_install_wizard is stubbed out — this covers the question → install → URL flow."""
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_wizard(version="3.2.8", port=7000)
        code, out = box.run_on_tty(
            "run_install_wizard() { echo '[fake] installing...'; }\naction_install",
            ["8443\n"],
        )
        assert code == 0, out
        assert "Which port should the web panel listen on?" in out, out
        assert "[fake] installing..." in out, out
        assert "PGClockMG is installed and running" in out, out
        assert re.search(r"Web panel\s+http://\S+:8443/\?token=[0-9a-f]{32,}", out), out
        assert "Port        8443" in out, out
        assert (box.install / ".access_token").is_file(), "token file must exist after install"
        token = (box.install / ".access_token").read_text(encoding="utf-8").strip()
        assert len(token) >= 32
        assert f"?token={token}" in out
        # Wizard install must not ask for the backup port.
        assert "BACKUP panel" not in out, out
    print("OK: install asks the port, installs, then shows the panel URL with token")


def test_install_failure_is_reported_without_killing_the_menu():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        res = box.run(
            "run_install_wizard() { echo boom >&2; return 1; }\n"
            "action_install || echo 'RETURNED-TO-MENU'",
            {"PG_MIGRATOR_PORT": "9099"},
        )
        out = plain(res.stdout + res.stderr)
        assert res.returncode == 0, out
        assert "Installation failed" in out, out
        assert "RETURNED-TO-MENU" in out, out
        assert "is installed and running" not in out, out
    print("OK: a failed install returns to the menu instead of exiting")


def test_install_stops_at_the_first_failing_step():
    """errexit must stay armed inside the subshell that runs the install steps."""
    with sandbox() as tmp:
        box = Sandbox(tmp)
        res = box.run(
            "run_install_wizard() { echo 'step one'; false; echo 'step two'; }\n"
            "run_action action_install\n"
            'echo "rc=$LAST_ACTION_RC"',
            {"PG_MIGRATOR_PORT": "9098"},
        )
        out = plain(res.stdout + res.stderr)
        assert res.returncode == 0, out
        assert "step one" in out, out
        assert "step two" not in out, out
        assert "Installation failed" in out, out
        assert "rc=1" in out, out
    print("OK: the install aborts on the first failing step")


# ── redirect server ───────────────────────────────────────────────────────────


def test_redirect_config_is_parsed():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_redirect(
            port=443,
            extra_ports=[2083, 2087],
            panel="hiddify",
            ssl={"enabled": True},
            redirect_base="https://pg.example.com:8000",
        )
        res = box.run(
            'redirect_load_config\n'
            'echo "port=$REDIRECT_PORT"\n'
            'echo "extra=$REDIRECT_EXTRA_PORTS"\n'
            'echo "panel=$REDIRECT_PANEL"\n'
            'echo "ssl=$REDIRECT_SSL"\n'
            'echo "base=$REDIRECT_BASE"\n'
            'echo "all=$(redirect_all_ports)"\n'
            'echo "pretty=$(format_ports "$(redirect_all_ports)")"'
        )
        out = res.stdout
        assert "port=443" in out, out
        assert "extra=2083 2087" in out, out
        assert "panel=hiddify" in out, out
        assert "ssl=true" in out, out
        assert "base=https://pg.example.com:8000" in out, out
        assert "all=443 2083 2087" in out, out
        assert "pretty=443, 2083, 2087" in out, out
    print("OK: redirect config parsing")


def test_redirect_menu_without_config_explains_itself():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        code, out = box.run_on_tty("redirect_menu", ["\n"])
        assert code == 0, out
        assert "No redirect server is configured" in out, out
        assert "3x-ui or Hiddify" in out, out
    print("OK: redirect menu explains when nothing is configured")


def test_redirect_menu_shows_actions_and_returns():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_redirect(port=59987, panel="x-ui")
        code, out = box.run_on_tty("redirect_menu", ["5\n"])
        assert code == 0, out
        assert "REDIRECT SERVER" in out
        assert "Restart redirect server" in out
        assert "Force restart" in out
        assert "Show recent logs" in out
        assert "Stop and disable" in out
        assert "x-ui" in out and "59987" in out
    print("OK: redirect menu renders status and actions")


def test_force_restart_can_be_declined():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_redirect(port=59986, panel="x-ui")
        code, out = box.run_on_tty("action_redirect_force_restart", ["n\n"])
        assert code == 0, out
        assert "Ports to free: 59986" in out, out
        assert "PasarGuard, your databases and migrated data are not touched" in out, out
        assert "Cancelled" in out, out
    print("OK: force restart asks before stopping the old panel")


def test_free_ports_is_safe_without_systemd_or_docker():
    """The sandbox has no systemd/docker — the helper must finish, not crash."""
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_redirect(port=59985, extra_ports=[59984], panel="x-ui")
        res = box.run("redirect_load_config; redirect_free_ports")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "Redirect ports released" in plain(res.stdout), res.stdout
    print("OK: freeing the redirect ports degrades gracefully")


def test_free_ports_kills_a_process_holding_the_port():
    """The old panel is simulated by a listener the script has to clear out."""
    import socket
    import sys

    with sandbox() as tmp:
        box = Sandbox(tmp)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import socket,time;"
                "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
                f"s.bind(('0.0.0.0',{port}));s.listen(5);time.sleep(120)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1.5)
            box.write_redirect(port=port, panel="x-ui")
            res = box.run("redirect_load_config; redirect_free_ports")
            assert res.returncode == 0, res.stdout + res.stderr
            time.sleep(1)
            assert holder.poll() is not None, "the process holding the port should be gone"
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.wait(timeout=10)
    print("OK: force restart clears a process squatting on the redirect port")


def test_healthz_probe_reports_dead_port():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        res = box.run("rc=0; redirect_healthz 59983 false || rc=$?; echo rc=$rc")
        assert "rc=1" in res.stdout, res.stdout
    print("OK: redirect health probe detects a dead port")


# ── uninstall ─────────────────────────────────────────────────────────────────


def test_uninstall_removes_service_and_keeps_backups():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_wizard(port=8443)
        box.write_redirect(port=2096, panel="x-ui")
        backups = box.install / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        (backups / "pg-backup.tar").write_text("data", encoding="utf-8")

        res = box.run("action_uninstall", {"PG_MIGRATOR_YES": "1"})
        out = plain(res.stdout)
        assert res.returncode == 0, out + res.stderr
        assert "PGClockMG removed." in out, out
        assert not box.install.exists(), "install dir should be gone"
        assert not (box.systemd / "pg-migrator.service").exists(), "unit should be gone"

        kept = list(box.keep.glob("pgclockmg-backups-*/pg-backup.tar"))
        assert kept, "backups should be copied out before deletion"

        # migration artefacts stay untouched
        assert (box.redirect / "config.json").exists()
        assert (box.systemd / "pg-redirect.service").exists()
        assert "still installed and untouched" in out
    print("OK: uninstall removes the wizard, keeps backups and the redirect server")


def test_uninstall_wizard_keeps_backup_service_when_both_present():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_wizard(port=8443)
        box.write_backup(port=7001)
        (box.backup / "backups" / "keep-me.tar").write_text("data", encoding="utf-8")

        res = box.run("action_uninstall_wizard", {"PG_MIGRATOR_YES": "1"})
        out = plain(res.stdout)
        assert res.returncode == 0, out + res.stderr
        assert "PGClockMG removed." in out, out
        assert not (box.systemd / "pg-migrator.service").exists()
        assert not box.install.exists()
        # Backup must remain completely untouched.
        assert (box.systemd / "pg-backup.service").exists()
        assert box.backup.exists()
        assert (box.backup / "backups" / "keep-me.tar").exists()
        assert "PGClockBackup is still installed" in out
    print("OK: uninstall wizard does not remove the backup service or install dir")


def test_uninstall_is_a_noop_when_not_installed():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.install.rmdir()
        res = box.run("action_uninstall", {"PG_MIGRATOR_YES": "1"})
        assert res.returncode == 0, res.stdout + res.stderr
        assert "nothing to remove" in plain(res.stdout), res.stdout
    print("OK: uninstall on a clean server does nothing")


def test_uninstall_can_be_declined():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_wizard()
        code, out = box.run_on_tty("action_uninstall", ["n\n"])
        assert code == 0, out
        assert "nothing was removed" in out, out
        assert box.install.exists()
        assert (box.systemd / "pg-migrator.service").exists()
    print("OK: uninstall requires an explicit yes")


def test_status_panel_shows_backup_row():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_wizard(version="3.2.8", port=8443)
        box.write_backup(version="4.0.1", port=7001)
        out = plain(box.run("print_status").stdout)
        assert "Wizard" in out and "3.2.8" in out and "8443" in out
        assert "Backup" in out and "4.0.1" in out and "7001" in out
    print("OK: status panel lists wizard and backup separately")


# ── menu loop & dispatch ──────────────────────────────────────────────────────


def test_menu_loop_rejects_unknown_option_then_exits():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        code, out = box.run_on_tty("menu_loop", ["x\n", "6\n"], settle=2.0)
        assert code == 0, out
        assert "Unknown option 'x'" in out, out
        assert "Bye." in out, out
    print("OK: menu loop handles a wrong answer and exits cleanly")


def test_menu_loop_opens_the_redirect_screen():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        box.write_redirect(port=59982, panel="x-ui")
        code, out = box.run_on_tty("menu_loop", ["5\n", "5\n", "6\n"], settle=1.5)
        assert code == 0, out
        assert "REDIRECT SERVER" in out, out
        assert "SYSTEM STATUS" in out, out
    print("OK: option 5 opens and leaves the redirect screen")


def test_menu_install_flow_end_to_end():
    """Menu → port question → install → success screen → back to the menu."""
    with sandbox() as tmp:
        box = Sandbox(tmp)
        code, out = box.run_on_tty(
            "run_install_wizard() { echo '[fake] installing...'; }\nmenu_loop",
            ["1\n", "8443\n", "\n", "6\n"],
            settle=1.5,
        )
        assert code == 0, out
        assert "[fake] installing..." in out, out
        assert re.search(r"Web panel\s+http://\S+:8443/\?token=[0-9a-f]{32,}", out), out
        assert "Press Enter to return to the menu" in out, out
        assert out.rstrip().endswith("Bye."), out[-200:]
    print("OK: install can be driven from the menu and returns to it")


def test_menu_loop_retries_before_giving_up_on_dead_tty():
    """A single failed read used to drop the operator straight back to the shell."""
    with sandbox() as tmp:
        box = Sandbox(tmp)
        res = box.run(
            "ask_tty() { return 1; }\n"
            "menu_loop; echo SURVIVED"
        )
        out = plain(res.stdout + res.stderr)
        assert res.returncode == 0, out
        assert "SURVIVED" in out, out
        assert "Could not read from the terminal" in out, out
        assert "No input received" in out, out
        assert out.count("No input received") >= 2
    print("OK: menu loop retries failed reads before exiting")


def test_cli_redirect_restart_needs_a_configured_redirect():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        res = box.run('run_action action_redirect_cli_restart; echo "rc=$LAST_ACTION_RC"')
        out = plain(res.stdout + res.stderr)
        assert res.returncode == 0, out
        assert "No redirect server is configured" in out, out
        assert "rc=1" in out, out
    print("OK: redirect-restart reports a missing redirect setup")


def test_unknown_cli_action_is_rejected():
    with sandbox() as tmp:
        box = Sandbox(tmp)
        # main() is only defined when the script is not sourced as a library
        code, out = box.run_on_tty(
            "main bogus-action", [], {"PG_MIGRATOR_INSTALL_LIB": "1"}
        )
        assert code != 0, out
        assert "Unknown command 'bogus-action'" in out, out
    print("OK: an unknown CLI action is rejected before anything runs")


def test_install_sh_still_syntax_checks():
    res = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    print("OK: install.sh parses")


if __name__ == "__main__":
    test_pasarguard_database_detection()
    test_pasarguard_version_from_compose()
    test_status_panel_reports_installed_stack()
    test_status_panel_warns_when_pasarguard_missing()
    test_status_panel_shows_backup_row()
    test_menu_lists_the_six_entries()
    test_install_asks_the_port_then_installs_and_prints_the_url()
    test_install_failure_is_reported_without_killing_the_menu()
    test_install_stops_at_the_first_failing_step()
    test_redirect_config_is_parsed()
    test_redirect_menu_without_config_explains_itself()
    test_redirect_menu_shows_actions_and_returns()
    test_force_restart_can_be_declined()
    test_free_ports_is_safe_without_systemd_or_docker()
    test_free_ports_kills_a_process_holding_the_port()
    test_healthz_probe_reports_dead_port()
    test_uninstall_removes_service_and_keeps_backups()
    test_uninstall_wizard_keeps_backup_service_when_both_present()
    test_uninstall_is_a_noop_when_not_installed()
    test_uninstall_can_be_declined()
    test_menu_loop_rejects_unknown_option_then_exits()
    test_menu_loop_opens_the_redirect_screen()
    test_menu_install_flow_end_to_end()
    test_cli_redirect_restart_needs_a_configured_redirect()
    test_unknown_cli_action_is_rejected()
    test_install_sh_still_syntax_checks()
    print("\nAll install menu tests passed.")
