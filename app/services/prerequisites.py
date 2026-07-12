"""System prerequisite checks."""

import os
import subprocess
from pathlib import Path

from app.config import (
    PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA,
    MARZBAN_DIR, MARZBAN_DATA, XUI_DB_PATHS, HIDDIFY_DIR,
)
from app.panels import PANELS, DATABASE_TYPES, TARGET_DB_RECOMMENDATIONS


def _run(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def is_pasarguard_installed() -> bool:
    return PASARGUARD_DIR.exists() and PASARGUARD_ENV.exists()


def is_marzban_installed() -> bool:
    return MARZBAN_DIR.exists() or MARZBAN_DATA.exists()


def is_hiddify_installed() -> bool:
    return HIDDIFY_DIR.exists()


def find_xui_db() -> Path | None:
    for p in XUI_DB_PATHS:
        if p.exists():
            return p
    return None


def is_docker_running() -> bool:
    ok, _ = _run(["docker", "info"])
    return ok


def is_root() -> bool:
    return os.geteuid() == 0 if hasattr(os, "geteuid") else True


def get_pasarguard_db_type() -> str | None:
    if not PASARGUARD_ENV.exists():
        return None
    text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    if "postgresql" in text or "asyncpg" in text:
        return "timescaledb" if "timescale" in text.lower() else "postgresql"
    if "mysql" in text or "asyncmy" in text:
        return "mysql"
    if "mariadb" in text:
        return "mariadb"
    if "sqlite" in text:
        return "sqlite"
    return None


def get_marzban_db_type() -> str | None:
    env_path = MARZBAN_DIR / ".env"
    if not env_path.exists():
        if (MARZBAN_DATA / "db.sqlite3").exists():
            return "sqlite"
        return None
    text = env_path.read_text(encoding="utf-8", errors="ignore")
    if "mysql" in text or "pymysql" in text:
        return "mysql"
    if "mariadb" in text:
        return "mariadb"
    if "sqlite" in text:
        return "sqlite"
    return None


def _suggest_marzban_mode(marzban_installed: bool, pg_installed: bool) -> str:
    if marzban_installed and not pg_installed:
        return "inplace"
    return "fresh"


def check_prerequisites(panel_id: str) -> dict:
    panel = PANELS.get(panel_id)
    if not panel:
        return {"ok": False, "checks": [], "message": {"en": "Invalid panel", "fa": "پنل نامعتبر", "ru": "Неверная панель"}}

    prereq = panel.prerequisites
    checks = []

    root_ok = is_root()
    checks.append({
        "id": "root",
        "label": {"en": "Root access", "fa": "دسترسی root", "ru": "Root доступ"},
        "ok": root_ok,
        "detail": {
            "en": "Required for .env and Docker changes" if not root_ok else "OK",
            "fa": "برای تغییر .env لازم است" if not root_ok else "فعال",
            "ru": "Нужен для изменений" if not root_ok else "OK",
        },
    })

    docker_ok = is_docker_running()
    checks.append({
        "id": "docker",
        "label": {"en": "Docker", "fa": "Docker", "ru": "Docker"},
        "ok": docker_ok,
        "detail": {
            "en": "Required for panel installation" if not docker_ok else "Running",
            "fa": "برای نصب پنل لازم است" if not docker_ok else "در حال اجرا",
            "ru": "Нужен для установки" if not docker_ok else "Работает",
        },
    })

    pg_installed = is_pasarguard_installed()
    marzban_installed = is_marzban_installed()
    hiddify_installed = is_hiddify_installed()
    xui_db = find_xui_db()

    # PasarGuard requirement
    if prereq.pasarguard_required:
        checks.append({
            "id": "pasarguard",
            "label": {"en": "PasarGuard installed", "fa": "PasarGuard نصب شده", "ru": "PasarGuard установлен"},
            "ok": pg_installed,
            "required_before": prereq.pasarguard_required_before,
            "detail": {
                "en": f"Install PasarGuard first at {PASARGUARD_DIR}" if not pg_installed else f"Found at {PASARGUARD_DIR}",
                "fa": "ابتدا PasarGuard را نصب کنید" if not pg_installed else f"نصب در {PASARGUARD_DIR}",
                "ru": "Сначала установите PasarGuard" if not pg_installed else f"Найден в {PASARGUARD_DIR}",
            },
        })
    elif pg_installed and panel_id == "marzban" and marzban_installed:
        checks.append({
            "id": "conflict",
            "label": {"en": "Install conflict", "fa": "تداخل نصب", "ru": "Конфликт установки"},
            "ok": False,
            "detail": {
                "en": "Both Marzban and PasarGuard found — use backup upload method",
                "fa": "هر دو نصب هستند — از آپلود بکاپ استفاده کنید",
                "ru": "Оба установлены — загрузите резервную копию",
            },
        })

    if not prereq.pasarguard_required and panel_id == "marzban":
        checks.append({
            "id": "marzban_or_backup",
            "label": {"en": "Marzban or backup", "fa": "Marzban یا بکاپ", "ru": "Marzban или копия"},
            "ok": marzban_installed,
            "optional": not marzban_installed,
            "detail": {
                "en": f"Marzban at {MARZBAN_DIR}" if marzban_installed else "Not found — upload backup in next step",
                "fa": f"Marzban در {MARZBAN_DIR}" if marzban_installed else "یافت نشد — بکاپ آپلود کنید",
                "ru": "Marzban найден" if marzban_installed else "Загрузите резервную копию",
            },
        })

    if panel_id == "3x-ui":
        checks.append({
            "id": "xui_db",
            "label": {"en": "3x-ui database", "fa": "دیتابیس 3x-ui", "ru": "База 3x-ui"},
            "ok": xui_db is not None,
            "optional": xui_db is None,
            "detail": {
                "en": f"Found: {xui_db}" if xui_db else "Upload x-ui.db in next step",
                "fa": f"یافت شد: {xui_db}" if xui_db else "x-ui.db را آپلود کنید",
                "ru": f"Найден: {xui_db}" if xui_db else "Загрузите x-ui.db",
            },
        })

    if panel_id == "hiddify":
        checks.append({
            "id": "hiddify",
            "label": {"en": "Hiddify Manager", "fa": "Hiddify", "ru": "Hiddify"},
            "ok": hiddify_installed,
            "optional": not hiddify_installed,
            "detail": {
                "en": f"At {HIDDIFY_DIR}" if hiddify_installed else "Upload MySQL dump in next step",
                "fa": "یا dump آپلود کنید" if not hiddify_installed else f"در {HIDDIFY_DIR}",
                "ru": "Загрузите дамп MySQL" if not hiddify_installed else "Найден",
            },
        })

    required_failed = [c for c in checks if not c.get("optional") and not c["ok"]]
    ok = len(required_failed) == 0

    return {
        "ok": ok,
        "checks": checks,
        "install_notes": prereq.install_notes,
        "prerequisites": {
            "pasarguard_required": prereq.pasarguard_required,
            "pasarguard_required_before": prereq.pasarguard_required_before,
            "source_panel_required": prereq.source_panel_required,
            "source_panel_required_before": prereq.source_panel_required_before,
        },
        "message": {
            "en": "Ready to migrate" if ok else "Missing prerequisites",
            "fa": "آماده مهاجرت" if ok else "پیش‌نیازهای ناقص",
            "ru": "Готово" if ok else "Не хватает условий",
        },
        "detected": {
            "pasarguard": pg_installed,
            "marzban": marzban_installed,
            "hiddify": hiddify_installed,
            "xui_db": str(xui_db) if xui_db else None,
            "pasarguard_db": get_pasarguard_db_type(),
            "marzban_db": get_marzban_db_type(),
            "suggested_marzban_mode": _suggest_marzban_mode(marzban_installed, pg_installed) if panel_id == "marzban" else None,
        },
    }


def get_recommended_target_dbs(source_panel: str, source_db: str) -> list[dict]:
    recs = TARGET_DB_RECOMMENDATIONS.get(source_db, ["sqlite", "timescaledb"])
    reasons = {
        "same": {
            "en": "Safest — same DB type, lowest risk",
            "fa": "ساده‌ترین — همان نوع دیتابیس",
            "ru": "Самый безопасный вариант",
        },
        "timescale": {
            "en": "Recommended for production — better stats",
            "fa": "توصیه برای پروداکشن",
            "ru": "Рекомендуется для продакшена",
        },
        "alt": {
            "en": "Alternative option",
            "fa": "گزینه جایگزین",
            "ru": "Альтернатива",
        },
    }
    result = []
    for i, db_id in enumerate(recs):
        info = DATABASE_TYPES.get(db_id, {})
        if i == 0 and source_db == db_id:
            reason = reasons["same"]
        elif db_id == "timescaledb":
            reason = reasons["timescale"]
        else:
            reason = reasons["alt"] if i > 0 else reasons["same"]
        result.append({
            "id": db_id,
            "name": info.get("name", {"en": db_id, "fa": db_id, "ru": db_id}),
            "recommended": i == 0,
            "reason": reason,
        })
    return result
