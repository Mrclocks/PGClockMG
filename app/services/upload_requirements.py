"""Define which files the wizard must collect per panel / migration context."""

from __future__ import annotations

from app.config import MARZBAN_DATA, MARZBAN_DIR
from app.services.prerequisites import (
    find_xui_db,
    is_hiddify_installed,
    is_marzban_installed,
    is_pasarguard_installed,
)

SLOT_DEFS: dict[str, dict] = {
    "bundle_zip": {
        "accept": [".zip"],
        "label": {
            "en": "Full backup ZIP",
            "fa": "بکاپ کامل (ZIP)",
            "ru": "Полная копия (ZIP)",
        },
        "hint": {
            "en": "All files in one zip — db, .env, certs, templates, xray_config",
            "fa": "همه فایل‌ها در یک zip — دیتابیس، .env، گواهی‌ها، قالب‌ها",
            "ru": "Все файлы в одном zip",
        },
    },
    "database": {
        "accept": [".sqlite3", ".db", ".sql"],
        "label": {
            "en": "Database file",
            "fa": "فایل دیتابیس",
            "ru": "Файл базы данных",
        },
        "hint": {
            "en": "db.sqlite3 (SQLite) or .sql dump (MySQL/MariaDB)",
            "fa": "db.sqlite3 برای SQLite یا فایل .sql برای MySQL",
            "ru": "db.sqlite3 или .sql дамп",
        },
    },
    "env": {
        "accept": [".env", ".txt"],
        "label": {
            "en": "Environment file (.env)",
            "fa": "فایل .env",
            "ru": "Файл .env",
        },
        "hint": {
            "en": "Marzban .env — subscription URL, SSL, DB password",
            "fa": ".env مرزبان — لینک اشتراک، SSL، رمز DB",
            "ru": ".env Marzban — подписка, SSL, пароль БД",
        },
    },
    "xray_config": {
        "accept": [".json"],
        "label": {
            "en": "Xray config",
            "fa": "تنظیمات Xray",
            "ru": "Конфиг Xray",
        },
        "hint": {
            "en": "xray_config.json from /var/lib/marzban/",
            "fa": "xray_config.json از /var/lib/marzban/",
            "ru": "xray_config.json из /var/lib/marzban/",
        },
    },
    "certs": {
        "accept": [".zip"],
        "label": {
            "en": "SSL certificates (zip)",
            "fa": "گواهی SSL (zip)",
            "ru": "SSL сертификаты (zip)",
        },
        "hint": {
            "en": "Zip of certs/ folder — keeps subscription links working",
            "fa": "zip پوشه certs/ — برای حفظ لینک‌ها",
            "ru": "zip папки certs/",
        },
    },
    "templates": {
        "accept": [".zip"],
        "label": {
            "en": "Subscription templates (zip)",
            "fa": "قالب اشتراک (zip)",
            "ru": "Шаблоны подписки (zip)",
        },
        "hint": {
            "en": "Zip of templates/ folder (v2ray or xray)",
            "fa": "zip پوشه templates/",
            "ru": "zip папки templates/",
        },
    },
}


def _slot(id_: str, required: bool, **extra) -> dict:
    base = {**SLOT_DEFS[id_], "id": id_, "required": required}
    base.update(extra)
    return base


def get_upload_requirements(
    panel_id: str,
    source_db: str | None = None,
    marzban_mode: str | None = None,
) -> dict:
    """Return upload policy and slot list for the wizard."""
    marzban_live = is_marzban_installed()
    xui_live = find_xui_db() is not None
    hiddify_live = is_hiddify_installed()
    pg_live = is_pasarguard_installed()

    mode = marzban_mode or "auto"
    if panel_id == "marzban" and mode == "auto":
        mode = "inplace" if marzban_live and not pg_live else "fresh"

    upload_mode = "none"
    slots: list[dict] = []
    reason = {"en": "Data found on server — upload optional", "fa": "داده روی سرور است — آپلود اختیاری", "ru": "Данные на сервере — загрузка опциональна"}

    if panel_id == "marzban":
        if mode == "inplace":
            upload_mode = "none" if marzban_live else "required"
            reason = {
                "en": "In-place: Marzban must be on this server" if marzban_live else "In-place: Marzban not found — upload backup",
                "fa": "درجا: Marzban روی سرور" if marzban_live else "درجا: Marzban نیست — بکاپ لازم",
                "ru": "На месте: Marzban на сервере" if marzban_live else "На месте: нужна копия",
            }
        else:
            if marzban_live and (MARZBAN_DATA / "db.sqlite3").exists():
                upload_mode = "optional"
            else:
                upload_mode = "required"
                reason = {
                    "en": "Upload Marzban backup — full ZIP or separate files below",
                    "fa": "بکاپ Marzban را آپلود کنید — zip کامل یا فایل‌های جدا",
                    "ru": "Загрузите копию Marzban — zip или отдельные файлы",
                }
            slots = _marzban_slots(source_db)

    elif panel_id == "3x-ui":
        if xui_live:
            upload_mode = "optional"
        else:
            upload_mode = "required"
            reason = {
                "en": "Upload x-ui.db from your 3x-ui server",
                "fa": "فایل x-ui.db را آپلود کنید",
                "ru": "Загрузите x-ui.db",
            }
            slots = [
                _slot("bundle_zip", False, exclusive=True),
                _slot("database", True, accept=[".db", ".sqlite3"], db_types=["sqlite"]),
            ]

    elif panel_id == "hiddify":
        if hiddify_live:
            upload_mode = "optional"
        else:
            upload_mode = "required"
            reason = {
                "en": "Upload Hiddify MySQL dump",
                "fa": "dump MySQL هیدیفای را آپلود کنید",
                "ru": "Загрузите дамп MySQL Hiddify",
            }
            slots = [
                _slot("bundle_zip", False, exclusive=True),
                _slot("database", True, accept=[".sql"], db_types=["mysql", "mariadb"]),
            ]

    elif panel_id == "pasarguard":
        upload_mode = "optional"
        reason = {
            "en": "Uses live PasarGuard DB on server — upload only if migrating from backup",
            "fa": "از DB زنده سرور استفاده می‌شود — فقط برای بکاپ آپلود کنید",
            "ru": "Используется БД на сервере — загрузка только для копии",
        }
        slots = [
            _slot("bundle_zip", False, exclusive=True),
            _slot("database", False, accept=[".sqlite3", ".sql"], db_types=["sqlite", "mysql", "mariadb", "postgresql"]),
        ]

    elif panel_id == "remnawave":
        upload_mode = "none"
        reason = {
            "en": "Remnawave uses API — no file upload needed",
            "fa": "رمناوی از API استفاده می‌کند — آپلود لازم نیست",
            "ru": "Remnawave через API — загрузка не нужна",
        }

    return {
        "upload_mode": upload_mode,
        "allow_zip": True,
        "allow_separate": True,
        "reason": reason,
        "slots": slots,
    }


def _marzban_slots(source_db: str | None) -> list[dict]:
    db = source_db or "sqlite"
    slots = [_slot("bundle_zip", False, exclusive=True)]
    if db == "sqlite":
        slots.append(_slot("database", True, accept=[".sqlite3", ".db"], db_types=["sqlite"]))
    else:
        slots.append(_slot("database", True, accept=[".sql"], db_types=["mysql", "mariadb"]))
    slots.extend([
        _slot("env", False),
        _slot("xray_config", False),
        _slot("certs", False),
        _slot("templates", False),
    ])
    return slots
