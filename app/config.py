import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("PG_MIGRATOR_HOME", Path(__file__).resolve().parent.parent))
UPLOAD_DIR = BASE_DIR / "uploads"
BACKUP_DIR = BASE_DIR / "backups"
LOG_DIR = BASE_DIR / "logs"
WORK_DIR = BASE_DIR / "work"
TOOLS_DIR = BASE_DIR / "tools"

PASARGUARD_DIR = Path("/opt/pasarguard")
PASARGUARD_ENV = PASARGUARD_DIR / ".env"
PASARGUARD_DATA = Path("/var/lib/pasarguard")

MARZBAN_DIR = Path("/opt/marzban")
MARZBAN_DATA = Path("/var/lib/marzban")

XUI_DB_PATHS = [
    Path("/etc/x-ui/x-ui.db"),
    Path("/usr/local/x-ui/bin/x-ui.db"),
    Path("/root/x-ui.db"),
]

HIDDIFY_DIR = Path("/opt/hiddify-manager")
HIDDIFY_MYSQL_PASS = HIDDIFY_DIR / "other/mysql/mysql_pass"

DEFAULT_WEB_PORT = 7000


def _web_port() -> int:
    """Port the installer chose for the wizard (systemd passes it in)."""
    raw = os.environ.get("PG_MIGRATOR_PORT", "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return DEFAULT_WEB_PORT


WEB_PORT = _web_port()

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
