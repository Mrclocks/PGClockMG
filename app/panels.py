"""Panel definitions and migration capability matrix."""

from app.models import PanelInfo

PANELS: dict[str, PanelInfo] = {
    "marzban": PanelInfo(
        id="marzban",
        name="Marzban",
        name_fa="مرزبان",
        icon="🛡️",
        support_level="full",
        subscription_preserved=True,
        description_fa=(
            "مهاجرت کامل با حفظ تمام کاربران، ترافیک، تنظیمات و "
            "لینک‌های اشتراک. بر اساس مستند رسمی PasarGuard."
        ),
        warnings_fa=[
            "برای مهاجرت درجا، Marzban باید روی همین سرور نصب باشد.",
            "اگر PasarGuard از قبل نصب است، از روش آپلود بکاپ استفاده کنید.",
        ],
        supported_source_dbs=["sqlite", "mysql", "mariadb"],
        requires_pasarguard=False,
        requires_source_installed=True,
    ),
    "3x-ui": PanelInfo(
        id="3x-ui",
        name="3x-UI / X-UI",
        name_fa="۳ایکس-یوآی",
        icon="📡",
        support_level="partial",
        subscription_preserved=False,
        description_fa=(
            "انتقال inboundها و کاربران. ادمین‌ها منتقل نمی‌شوند. "
            "لینک اشتراک تغییر می‌کند — سرور ریدایرکت قابل نصب است."
        ),
        warnings_fa=[
            "لینک‌های اشتراک کاربران تغییر خواهند کرد.",
            "اکانت‌های ادمین باید دستی در PasarGuard ساخته شوند.",
            "PasarGuard باید قبل از مهاجرت نصب شده باشد.",
            "فقط دیتابیس SQLite پشتیبانی می‌شود.",
        ],
        supported_source_dbs=["sqlite"],
        requires_pasarguard=True,
        requires_source_installed=False,
    ),
    "hiddify": PanelInfo(
        id="hiddify",
        name="Hiddify Manager",
        name_fa="هیدیفای",
        icon="🔮",
        support_level="experimental",
        subscription_preserved=False,
        description_fa=(
            "پشتیبانی آزمایشی — انتقال کاربران از دیتابیس MySQL/MariaDB. "
            "تنظیمات پیچیده Hiddify ممکن است کامل منتقل نشود."
        ),
        warnings_fa=[
            "ابزار رسمی مهاجرت Hiddify وجود ندارد.",
            "لینک‌های اشتراک تغییر خواهند کرد.",
            "تنظیمات پروتکل و دامنه باید دستی بازتنظیم شوند.",
            "PasarGuard باید قبل از مهاجرت نصب شده باشد.",
        ],
        supported_source_dbs=["mysql", "mariadb"],
        requires_pasarguard=True,
        requires_source_installed=False,
    ),
    "pasarguard": PanelInfo(
        id="pasarguard",
        name="PasarGuard (DB Migration)",
        name_fa="پاسارگارد (تغییر دیتابیس)",
        icon="🔄",
        description_fa=(
            "انتقال داده‌های PasarGuard بین انواع دیتابیس "
            "(SQLite, MySQL, PostgreSQL, TimescaleDB) با ابزار رسمی."
        ),
        support_level="db_only",
        subscription_preserved=True,
        warnings_fa=[
            "PasarGuard باید روی سرور نصب باشد.",
            "نسخه PasarGuard مبدأ و مقصد باید یکسان باشد.",
        ],
        supported_source_dbs=["sqlite", "mysql", "mariadb", "postgresql", "timescaledb"],
        requires_pasarguard=True,
        requires_source_installed=False,
    ),
}

# Fix duplicate name_fa in pasarguard - let me fix that in the write

DATABASE_TYPES = {
    "sqlite": {"name": "SQLite", "name_fa": "SQLite", "default_port": None},
    "mysql": {"name": "MySQL", "name_fa": "MySQL", "default_port": 3306},
    "mariadb": {"name": "MariaDB", "name_fa": "MariaDB", "default_port": 3306},
    "postgresql": {"name": "PostgreSQL", "name_fa": "PostgreSQL", "default_port": 5432},
    "timescaledb": {"name": "TimescaleDB", "name_fa": "TimescaleDB (توصیه‌شده)", "default_port": 5432},
}

TARGET_DB_RECOMMENDATIONS = {
    "sqlite": ["sqlite", "timescaledb"],
    "mysql": ["mysql", "timescaledb", "postgresql"],
    "mariadb": ["mariadb", "mysql", "timescaledb"],
    "postgresql": ["postgresql", "timescaledb"],
    "timescaledb": ["timescaledb"],
}
