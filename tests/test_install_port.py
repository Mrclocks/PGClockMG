"""Web-panel port selection in install.sh.

The installer asks which port the wizard should listen on. Sourcing the script
with PG_MIGRATOR_INSTALL_LIB=1 loads the helpers without running the install, so
the prompt logic can be exercised here (a pty stands in for the operator).
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"


def _env(extra: dict | None) -> dict:
    base = {k: v for k, v in os.environ.items() if k != "PG_MIGRATOR_PORT"}
    return {**base, "PG_MIGRATOR_INSTALL_LIB": "1", **(extra or {})}


def run_bash(snippet: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Source install.sh as a library, then run `snippet` against its helpers."""
    full_env = _env(env)
    return subprocess.run(
        ["bash", "-c", f'source "{INSTALL_SH}"\n{snippet}'],
        capture_output=True,
        text=True,
        env=full_env,
        start_new_session=True,  # no controlling terminal -> /dev/tty is unusable
    )


def run_bash_on_tty(snippet: str, keystrokes: str, env: dict | None = None) -> tuple[int, str]:
    """Same, but stdin is a pty so the script takes its interactive path."""
    full_env = _env(env)
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", "-c", f'source "{INSTALL_SH}"\n{snippet}'],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=full_env,
        )
        os.write(master, keystrokes.encode())
        out, _ = proc.communicate(timeout=30)
        return proc.returncode, out
    finally:
        os.close(master)
        os.close(slave)


def test_valid_port_accepts_and_rejects():
    for good in ("1", "80", "7000", "8443", "65535"):
        assert run_bash(f'valid_port "{good}"').returncode == 0, good
    for bad in ("", "0", "65536", "-1", "70o0", "8080a", "1.5", "99999999"):
        assert run_bash(f'valid_port "{bad}"').returncode != 0, bad
    print("OK: valid_port range check")


def test_detect_installed_port_reads_existing_unit():
    with tempfile.TemporaryDirectory() as tmp:
        unit = Path(tmp) / "pg-migrator.service"
        unit.write_text(
            "[Service]\n"
            "ExecStart=/opt/pg-migrator/venv/bin/python -m uvicorn app.main:app "
            "--host 0.0.0.0 --port 8443\n",
            encoding="utf-8",
        )
        res = run_bash(f'detect_installed_port "{unit}"')
        assert res.stdout.strip() == "8443", res.stdout

        missing = run_bash(f'detect_installed_port "{tmp}/nope.service"')
        assert missing.returncode == 0 and missing.stdout.strip() == ""
    print("OK: detect_installed_port keeps the previous port")


def test_env_var_selects_port_without_prompting():
    res = run_bash('select_web_port >/dev/null; echo "$WEB_PORT"', {"PG_MIGRATOR_PORT": "8443"})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "8443", res.stdout
    print("OK: PG_MIGRATOR_PORT drives non-interactive installs")


def test_invalid_env_var_aborts_install():
    res = run_bash('select_web_port; echo "$WEB_PORT"', {"PG_MIGRATOR_PORT": "not-a-port"})
    assert res.returncode != 0, res.stdout
    assert "not a valid port" in res.stdout + res.stderr
    print("OK: invalid PG_MIGRATOR_PORT fails loudly")


def test_no_terminal_falls_back_to_default():
    env = _env(None)
    res = subprocess.run(
        ["bash", "-c", f'source "{INSTALL_SH}"\nselect_web_port >/dev/null 2>&1; echo "$WEB_PORT"'],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "7000", res.stdout
    print("OK: unattended install keeps port 7000")


def test_prompt_accepts_custom_port():
    code, out = run_bash_on_tty('select_web_port; echo "PORT=$WEB_PORT"', "8443\n")
    assert code == 0, out
    assert "PORT=8443" in out, out
    print("OK: answering the prompt sets the port")


def test_prompt_empty_answer_keeps_default():
    code, out = run_bash_on_tty('select_web_port; echo "PORT=$WEB_PORT"', "\n")
    assert code == 0, out
    assert "PORT=7000" in out, out
    print("OK: pressing Enter keeps 7000")


def test_prompt_reasks_after_invalid_answer():
    code, out = run_bash_on_tty('select_web_port; echo "PORT=$WEB_PORT"', "70000\nabc\n9090\n")
    assert code == 0, out
    assert "PORT=9090" in out, out
    assert "not a valid port" in out, out
    print("OK: invalid answers are re-asked, not accepted")


def test_prompt_rejects_a_busy_port():
    if not (shutil.which("ss") or shutil.which("netstat")):
        print("SKIP: no ss/netstat to detect busy ports")
        return
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        code, out = run_bash_on_tty('select_web_port; echo "PORT=$WEB_PORT"', f"{busy}\n9091\n")
    assert code == 0, out
    assert "PORT=9091" in out, out
    assert "already used" in out, out
    print("OK: a busy port is refused")


def test_service_unit_and_firewall_use_the_chosen_port():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "Environment=PG_MIGRATOR_PORT=${WEB_PORT}" in text
    assert "--port ${WEB_PORT}" in text
    assert 'ufw allow "${WEB_PORT}/tcp"' in text
    assert not re.search(r"--port 7000", text)
    print("OK: unit file, firewall and banner follow WEB_PORT")


def test_app_config_reads_the_port_from_the_environment():
    with tempfile.TemporaryDirectory() as tmp:
        snippet = "import app.config as c; print(c.WEB_PORT)"
        for value, expected in (("8443", "8443"), ("", "7000"), ("abc", "7000"), ("0", "7000")):
            env = {**os.environ, "PG_MIGRATOR_HOME": tmp, "PG_MIGRATOR_PORT": value}
            res = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True, text=True, cwd=str(ROOT), env=env,
            )
            assert res.returncode == 0, res.stderr
            assert res.stdout.strip() == expected, f"{value!r} -> {res.stdout!r}"
    print("OK: app.config.WEB_PORT follows PG_MIGRATOR_PORT")


if __name__ == "__main__":
    test_valid_port_accepts_and_rejects()
    test_detect_installed_port_reads_existing_unit()
    test_env_var_selects_port_without_prompting()
    test_invalid_env_var_aborts_install()
    test_no_terminal_falls_back_to_default()
    test_prompt_accepts_custom_port()
    test_prompt_empty_answer_keeps_default()
    test_prompt_reasks_after_invalid_answer()
    test_prompt_rejects_a_busy_port()
    test_service_unit_and_firewall_use_the_chosen_port()
    test_app_config_reads_the_port_from_the_environment()
    print("\nAll install port tests passed.")
