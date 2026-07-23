"""Panel definitions and migration capability matrix."""

from app.models import PanelInfo, PanelPrerequisites

# Official install engines (links-only guide — wizard never runs the installer)
PASARGUARD_INSTALL_DBS = ["timescaledb", "postgresql", "mysql", "mariadb", "sqlite"]

SCRIPT_URL = "https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh"
DOCS_INSTALL_URL = "https://docs.pasarguard.org/en/panel/installation/"
DOCS_NODE_URL = "https://github.com/PasarGuard/node"
PANEL_GITHUB_URL = "https://github.com/PasarGuard/panel"

# Official one-liners from docs.pasarguard.org (run on the server as root)
PASARGUARD_INSTALL_COMMANDS: dict[str, dict] = {
    "timescaledb": {
        "label": {"en": "TimescaleDB (Recommended)", "fa": "TimescaleDB (پیشنهادی)", "ru": "TimescaleDB (рекомендуется)"},
        "desc": {
            "en": "Best for production — time-series optimized PostgreSQL.",
            "fa": "بهترین برای پروداکشن — PostgreSQL بهینه‌شده برای سری زمانی.",
            "ru": "Лучший для продакшена — PostgreSQL для временных рядов.",
        },
        "cmd": (
            f'curl -fsSL {SCRIPT_URL} -o /tmp/pg.sh \\\n'
            f'  && sudo bash /tmp/pg.sh install --database timescaledb'
        ),
    },
    "postgresql": {
        "label": {"en": "PostgreSQL", "fa": "PostgreSQL", "ru": "PostgreSQL"},
        "desc": {
            "en": "Standard PostgreSQL — advanced features and scalability.",
            "fa": "PostgreSQL استاندارد — امکانات پیشرفته و مقیاس‌پذیری.",
            "ru": "Обычный PostgreSQL — расширенные возможности.",
        },
        "cmd": (
            f'curl -fsSL {SCRIPT_URL} -o /tmp/pg.sh \\\n'
            f'  && sudo bash /tmp/pg.sh install --database postgresql'
        ),
    },
    "mysql": {
        "label": {"en": "MySQL", "fa": "MySQL", "ru": "MySQL"},
        "desc": {
            "en": "Classic MySQL — common for production panels.",
            "fa": "MySQL کلاسیک — رایج برای پنل‌های پروداکشن.",
            "ru": "Классический MySQL — часто для продакшена.",
        },
        "cmd": (
            f'curl -fsSL {SCRIPT_URL} -o /tmp/pg.sh \\\n'
            f'  && sudo bash /tmp/pg.sh install --database mysql'
        ),
    },
    "mariadb": {
        "label": {"en": "MariaDB", "fa": "MariaDB", "ru": "MariaDB"},
        "desc": {
            "en": "Open-source MySQL-compatible engine.",
            "fa": "موتور متن‌باز سازگار با MySQL.",
            "ru": "Совместимый с MySQL open-source движок.",
        },
        "cmd": (
            f'curl -fsSL {SCRIPT_URL} -o /tmp/pg.sh \\\n'
            f'  && sudo bash /tmp/pg.sh install --database mariadb'
        ),
    },
    "sqlite": {
        "label": {"en": "SQLite", "fa": "SQLite", "ru": "SQLite"},
        "desc": {
            "en": "Simple file DB — small deployments and testing only.",
            "fa": "دیتابیس فایل ساده — فقط تست و استقرار کوچک.",
            "ru": "Файловая БД — только тесты и малые установки.",
        },
        "cmd": (
            f'curl -fsSL {SCRIPT_URL} -o /tmp/pg.sh \\\n'
            f'  && sudo bash /tmp/pg.sh install'
        ),
    },
}

OWNER_TEMP_KEY_CMD = "pasarguard cli generate-temp-key"
SSH_TUNNEL_CMD = "ssh -L 8000:localhost:8000 user@serverip"


def can_convert_databases(source_db: str | None, target_db: str | None) -> bool:
    """sqlite → any; never non-sqlite → sqlite; other engines convert to each other."""
    if not source_db or not target_db:
        return False
    if source_db == target_db:
        return True
    soft = {source_db, target_db}
    if soft <= {"mysql", "mariadb"} or soft <= {"postgresql", "timescaledb"}:
        return True
    if target_db == "sqlite" and source_db != "sqlite":
        return False
    engines = {"sqlite", "mysql", "mariadb", "postgresql", "timescaledb"}
    if source_db not in engines or target_db not in engines:
        return False
    return True


PANELS: dict[str, PanelInfo] = {
    "marzban": PanelInfo(
        id="marzban",
        name={"en": "Marzban", "fa": "Marzban", "ru": "Marzban"},
        icon="🛡️",
        support_level="full",
        subscription_mode="native",
        description={
            "en": "Migrate Marzban to an already-installed PasarGuard. Upload backup; source DB is auto-detected.",
            "fa": "مهاجرت Marzban به PasarGuard نصب‌شده. بکاپ آپلود کنید؛ نوع DB مبدأ خودکار تشخیص داده می‌شود.",
            "ru": "Миграция Marzban в установленный PasarGuard. Загрузите копию; БД источника определяется автоматически.",
        },
        warnings={
            "en": [
                "PasarGuard MUST be installed on this server BEFORE running this wizard.",
                "Upload Marzban backup (ZIP or separate files) — source DB is detected automatically.",
                "Select the database you chose during PasarGuard install (may differ from Marzban).",
                "Cross-DB uses two-phase engine: upgrade source to head, then copy head→head (no data loss).",
            ],
            "fa": [
                "PasarGuard باید قبل از اجرای این ویزارد روی سرور نصب شده باشد.",
                "بکاپ Marzban را آپلود کنید — نوع DB مبدأ خودکار تشخیص داده می‌شود.",
                "دیتابیسی را انتخاب کنید که هنگام نصب PasarGuard انتخاب کردید (ممکن است با Marzban فرق داشته باشد).",
                "مهاجرت بین DB با موتور دو‌فازی: ارتقا به head سپس کپی هم‌تراز — بدون از دست رفتن داده.",
            ],
            "ru": [
                "PasarGuard ДОЛЖЕН быть установлен ДО запуска мастера.",
                "Загрузите копию Marzban — БД источника определяется автоматически.",
                "Выберите БД, которую указали при установке PasarGuard.",
                "Смена СУБД двухфазным движком: upgrade до head, затем копирование head→head.",
            ],
        },
        prerequisites=PanelPrerequisites(
            pasarguard_required=True,
            pasarguard_required_before=True,
            source_panel_required=False,
            source_panel_required_before=False,
            install_notes={
                "en": "1) Install PasarGuard manually (choose database during install). 2) Upload Marzban backup in wizard.",
                "fa": "۱) PasarGuard را دستی نصب کنید (دیتابیس را در نصب انتخاب کنید). ۲) بکاپ Marzban را در ویزارد آپلود کنید.",
                "ru": "1) Установите PasarGuard вручную. 2) Загрузите копию Marzban в мастере.",
            },
        ),
        supported_source_dbs=["sqlite", "mysql", "mariadb"],
    ),
    "3x-ui": PanelInfo(
        id="3x-ui",
        name={"en": "X-UI", "fa": "X-UI", "ru": "X-UI"},
        icon="📡",
        support_level="partial",
        subscription_mode="redirect",
        description={
            "en": "Migrates inbounds and users. Admin accounts are not migrated. Old subscription URLs work via redirect server (official PasarGuard tool).",
            "fa": "انتقال inbound و کاربران. ادمین منتقل نمی‌شود. لینک‌های قدیمی با redirect server کار می‌کنند.",
            "ru": "Миграция inbound и пользователей. Админы не переносятся. Старые ссылки работают через redirect server.",
        },
        warnings={
            "en": [
                "PasarGuard MUST be installed on this server BEFORE migration.",
                "SQLite only (x-ui.db).",
                "Install redirect server (checked by default) to keep old /sub/{token} links working.",
                "Create admin manually after migration: pasarguard cli generate-temp-key",
            ],
            "fa": [
                "PasarGuard باید قبل از مهاجرت روی این سرور نصب باشد.",
                "فقط SQLite (x-ui.db).",
                "سرور ریدایرکت را نصب کنید (پیش‌فرض فعال) تا لینک‌های قدیمی کار کنند.",
                "ادمین را دستی بسازید: pasarguard cli generate-temp-key",
            ],
            "ru": [
                "PasarGuard ДОЛЖЕН быть установлен ДО миграции.",
                "Только SQLite (x-ui.db).",
                "Установите redirect server (по умолчанию) для старых ссылок.",
                "Создайте админа: pasarguard cli generate-temp-key",
            ],
        },
        prerequisites=PanelPrerequisites(
            pasarguard_required=True,
            pasarguard_required_before=True,
            source_panel_required=False,
            source_panel_required_before=False,
            install_notes={
                "en": "Step 1: Install PasarGuard (empty, fresh install). Step 2: Provide x-ui.db from this server or upload backup.",
                "fa": "مرحله ۱: PasarGuard را نصب کنید. مرحله ۲: فایل x-ui.db را بدهید یا آپلود کنید.",
                "ru": "Шаг 1: Установите PasarGuard. Шаг 2: Предоставьте x-ui.db или загрузите копию.",
            },
        ),
        supported_source_dbs=["sqlite"],
    ),
    "remnawave": PanelInfo(
        id="remnawave",
        name={"en": "Remnawave", "fa": "Remnawave", "ru": "Remnawave"},
        icon="🌊",
        support_level="experimental",
        subscription_mode="changed",
        description={
            "en": "Experimental API-based user migration. Nodes, squads and inbounds must be reconfigured manually.",
            "fa": "مهاجرت آزمایشی از طریق API. نودها و inbound باید دستی تنظیم شوند.",
            "ru": "Экспериментальная миграция через API. Ноды и inbound настраиваются вручную.",
        },
        warnings={
            "en": [
                "No official PasarGuard migration tool for Remnawave.",
                "PasarGuard MUST be installed before migration.",
                "Requires Remnawave API URL and token.",
                "Subscription links will change.",
            ],
            "fa": [
                "ابزار رسمی مهاجرت Remnawave وجود ندارد.",
                "PasarGuard باید قبل از مهاجرت نصب باشد.",
                "نیاز به URL و API Token رمناوی.",
                "لینک اشتراک تغییر می‌کند.",
            ],
            "ru": [
                "Официального инструмента миграции Remnawave нет.",
                "PasarGuard должен быть установлен заранее.",
                "Нужны URL и API Token Remnawave.",
                "Ссылки подписки изменятся.",
            ],
        },
        prerequisites=PanelPrerequisites(
            pasarguard_required=True,
            pasarguard_required_before=True,
            source_panel_required=False,
            source_panel_required_before=False,
            install_notes={
                "en": "Install PasarGuard first. Remnawave can run on same or remote server — provide API URL + token in wizard.",
                "fa": "ابتدا PasarGuard را نصب کنید. Remnawave می‌تواند روی همین یا سرور دیگر باشد.",
                "ru": "Сначала установите PasarGuard. Remnawave может быть на этом или другом сервере.",
            },
        ),
        supported_source_dbs=["postgresql"],
    ),
    "hiddify": PanelInfo(
        id="hiddify",
        name={"en": "Hiddify Manager", "fa": "Hiddify Manager", "ru": "Hiddify Manager"},
        icon="🔮",
        support_level="experimental",
        subscription_mode="changed",
        description={
            "en": "Experimental user migration from MySQL/MariaDB. Complex Hiddify settings may not transfer.",
            "fa": "انتقال آزمایشی کاربران از MySQL. تنظیمات پیچیده منتقل نمی‌شوند.",
            "ru": "Экспериментальный перенос пользователей из MySQL.",
        },
        warnings={
            "en": [
                "No official migration tool.",
                "PasarGuard MUST be installed before migration.",
                "Subscription links will change.",
            ],
            "fa": [
                "ابزار رسمی وجود ندارد.",
                "PasarGuard باید قبل از مهاجرت نصب باشد.",
                "لینک اشتراک تغییر می‌کند.",
            ],
            "ru": [
                "Официального инструмента нет.",
                "PasarGuard должен быть установлен заранее.",
                "Ссылки подписки изменятся.",
            ],
        },
        prerequisites=PanelPrerequisites(
            pasarguard_required=True,
            pasarguard_required_before=True,
            source_panel_required=False,
            source_panel_required_before=False,
            install_notes={
                "en": "Install PasarGuard first. Provide Hiddify MySQL dump or use live database on this server.",
                "fa": "ابتدا PasarGuard را نصب کنید. dump MySQL هیدیفای را بدهید.",
                "ru": "Сначала PasarGuard. Предоставьте дамп MySQL Hiddify.",
            },
        ),
        supported_source_dbs=["mysql", "mariadb"],
    ),
    "pasarguard": PanelInfo(
        id="pasarguard",
        name={"en": "PasarGuard (DB only)", "fa": "PasarGuard (DB only)", "ru": "PasarGuard (DB only)"},
        icon="🔄",
        support_level="db_only",
        subscription_mode="native",
        description={
            "en": "Migrate PasarGuard data between database engines (SQLite, MySQL, PostgreSQL, TimescaleDB).",
            "fa": "انتقال داده PasarGuard بین انواع دیتابیس.",
            "ru": "Миграция данных PasarGuard между СУБД.",
        },
        warnings={
            "en": [
                "PasarGuard MUST already be installed and running.",
                "Source and target app version should match.",
            ],
            "fa": [
                "PasarGuard باید از قبل نصب و در حال اجرا باشد.",
                "نسخه مبدأ و مقصد باید یکسان باشد.",
            ],
            "ru": [
                "PasarGuard должен быть уже установлен.",
                "Версии должны совпадать.",
            ],
        },
        prerequisites=PanelPrerequisites(
            pasarguard_required=True,
            pasarguard_required_before=True,
            source_panel_required=False,
            source_panel_required_before=False,
            install_notes={
                "en": "PasarGuard must be running. This wizard only changes the database backend.",
                "fa": "PasarGuard باید در حال اجرا باشد. فقط دیتابیس تغییر می‌کند.",
                "ru": "PasarGuard должен работать. Меняется только база данных.",
            },
        ),
        supported_source_dbs=["sqlite", "mysql", "mariadb", "postgresql", "timescaledb"],
    ),
}

DATABASE_TYPES = {
    "sqlite": {"name": {"en": "SQLite", "fa": "SQLite", "ru": "SQLite"}},
    "mysql": {"name": {"en": "MySQL", "fa": "MySQL", "ru": "MySQL"}},
    "mariadb": {"name": {"en": "MariaDB", "fa": "MariaDB", "ru": "MariaDB"}},
    "postgresql": {"name": {"en": "PostgreSQL", "fa": "PostgreSQL", "ru": "PostgreSQL"}},
    "timescaledb": {"name": {"en": "TimescaleDB (recommended)", "fa": "TimescaleDB (توصیه‌شده)", "ru": "TimescaleDB (рекомендуется)"}},
}

TARGET_DB_RECOMMENDATIONS = {
    "sqlite": ["timescaledb", "postgresql", "mysql", "mariadb", "sqlite"],
    "mysql": ["mysql", "mariadb", "timescaledb", "postgresql"],
    "mariadb": ["mariadb", "mysql", "timescaledb", "postgresql"],
    "postgresql": ["postgresql", "timescaledb", "mysql", "mariadb"],
    "timescaledb": ["timescaledb", "postgresql", "mysql", "mariadb"],
}

SUBSCRIPTION_LABELS = {
    "native": {"en": "Links preserved", "fa": "لینک‌ها حفظ می‌شوند", "ru": "Ссылки сохраняются"},
    "redirect": {"en": "Links preserved via redirect", "fa": "لینک‌ها با redirect حفظ می‌شوند", "ru": "Ссылки через redirect"},
    "changed": {"en": "Links will change", "fa": "لینک‌ها تغییر می‌کنند", "ru": "Ссылки изменятся"},
}
