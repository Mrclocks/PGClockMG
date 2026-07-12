import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("PG_MIGRATOR_HOME", Path(__file__).resolve().parent.parent))
UPLOAD_DIR = BASE_DIR / "uploads"
BACKUP_DIR = BASE_DIR / "backups"
LOG_DIR = BASE_DIR / "logs"
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

WEB_PORT = 7000

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
