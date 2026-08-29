import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("PG_MIGRATOR_HOME", Path(__file__).resolve().parent.parent))
# Backup panel may use its own root (/opt/pg-backup) via PG_BACKUP_HOME.
BACKUP_HOME = Path(
    os.environ.get("PG_BACKUP_HOME")
    or os.environ.get("PG_MIGRATOR_HOME")
    or BASE_DIR
)
UPLOAD_DIR = BASE_DIR / "uploads"
BACKUP_DIR = BACKUP_HOME / "backups"
LOG_DIR = BASE_DIR / "logs"
WORK_DIR = BASE_DIR / "work"
TOOLS_DIR = BASE_DIR / "tools"
BACKUP_PANEL_DIR = BACKUP_HOME / "backup_panel"
BACKUP_SETTINGS_FILE = BACKUP_PANEL_DIR / "settings.json"
BACKUP_PASSWORD_FILE = BACKUP_PANEL_DIR / ".password"
BACKUP_SECRET_FILE = BACKUP_PANEL_DIR / ".session_secret"
BACKUP_SETUP_TOKEN_FILE = BACKUP_PANEL_DIR / ".setup_token"
BACKUP_JOBS_DIR = BACKUP_PANEL_DIR / "jobs"

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
DEFAULT_BACKUP_PORT = 7001
TELEGRAM_BOT_MAX_BYTES = 49 * 1024 * 1024  # stay under Telegram bot 50MB limit


def _port_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return default


def _web_port() -> int:
    """Port the installer chose for the wizard (systemd passes it in)."""
    return _port_from_env("PG_MIGRATOR_PORT", DEFAULT_WEB_PORT)


def _backup_port() -> int:
    """Port for the backup management panel."""
    return _port_from_env("PG_BACKUP_PORT", DEFAULT_BACKUP_PORT)


WEB_PORT = _web_port()
BACKUP_PORT = _backup_port()

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_PANEL_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_JOBS_DIR.mkdir(parents=True, exist_ok=True)
