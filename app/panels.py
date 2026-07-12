"""Panel definitions and migration capability matrix."""

from app.models import PanelInfo, PanelPrerequisites

PANELS: dict[str, PanelInfo] = {
    "marzban": PanelInfo(
        id="marzban",
        name={"en": "Marzban", "fa": "مرزبان", "ru": "Marzban"},
        icon="🛡️",
        support_level="full",
        subscription_mode="native",
        description={
            "en": "Two official methods: in-place rename (Marzban on server) or fresh PasarGuard install with backup import.",
            "fa": "دو روش رسمی: مهاجرت درجا (مرزبان روی سرور) یا نصب تازه PasarGuard با بکاپ.",
            "ru": "Два официальных метода: на месте (Marzban на сервере) или чистая установка PasarGuard с копией.",
        },
        warnings={
            "en": [
                "Method 1 (in-place): Marzban on THIS server, PasarGuard NOT installed yet.",
                "Method 2 (fresh): PasarGuard already installed OR upload Marzban backup (db.sqlite3 / .sql).",
                "Cross-DB (e.g. SQLite → TimescaleDB) uses official db-migrations tool automatically.",
                "Docker is required.",
            ],
            "fa": [
                "روش ۱ (درجا): مرزبان روی همین سرور، PasarGuard نصب نباشد.",
                "روش ۲ (تازه): PasarGuard نصب است یا بکاپ مرزبان آپلود کنید.",
                "تغییر نوع DB (مثلاً SQLite به TimescaleDB) با ابزار رسمی db-migrations انجام می‌شود.",
                "Docker لازم است.",
            ],
            "ru": [
                "Метод 1 (на месте): Marzban на ЭТОМ сервере, PasarGuard НЕ установлен.",
                "Метод 2 (чистый): PasarGuard установлен ИЛИ загрузите копию Marzban.",
                "Смена СУБД (SQLite → TimescaleDB) через официальный db-migrations.",
                "Требуется Docker.",
            ],
        },
        prerequisites=PanelPrerequisites(
            pasarguard_required=False,
            pasarguard_required_before=False,
            source_panel_required=True,
            source_panel_required_before=True,
            install_notes={
                "en": "Choose migration method after selecting Marzban: in-place (Marzban on server) or fresh (PasarGuard install + backup).",
                "fa": "بعد از انتخاب مرزبان روش را مشخص کنید: درجا (مرزبان روی سرور) یا تازه (نصب PasarGuard + بکاپ).",
                "ru": "После выбора Marzban укажите метод: на месте или чистая установка PasarGuard с копией.",
            },
        ),
        supported_source_dbs=["sqlite", "mysql", "mariadb"],
    ),
    "3x-ui": PanelInfo(
        id="3x-ui",
        name={"en": "3x-UI / X-UI", "fa": "۳ایکس-یوآی", "ru": "3x-UI / X-UI"},
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
        name={"en": "Remnawave", "fa": "رمناوی", "ru": "Remnawave"},
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
        name={"en": "Hiddify Manager", "fa": "هیدیفای", "ru": "Hiddify Manager"},
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
        name={"en": "PasarGuard (DB only)", "fa": "پاسارگارد (تغییر DB)", "ru": "PasarGuard (только БД)"},
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
    "sqlite": ["sqlite", "timescaledb"],
    "mysql": ["mysql", "timescaledb", "postgresql"],
    "mariadb": ["mariadb", "mysql", "timescaledb"],
    "postgresql": ["postgresql", "timescaledb"],
    "timescaledb": ["timescaledb"],
}

SUBSCRIPTION_LABELS = {
    "native": {"en": "Links preserved", "fa": "لینک‌ها حفظ می‌شوند", "ru": "Ссылки сохраняются"},
    "redirect": {"en": "Links preserved via redirect", "fa": "لینک‌ها با redirect حفظ می‌شوند", "ru": "Ссылки через redirect"},
    "changed": {"en": "Links will change", "fa": "لینک‌ها تغییر می‌کنند", "ru": "Ссылки изменятся"},
}
