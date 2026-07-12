"""System prerequisite checks."""

import os
import subprocess
from pathlib import Path

from app.config import (
    PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA,
    MARZBAN_DIR, MARZBAN_DATA, XUI_DB_PATHS, HIDDIFY_DIR,
)
from app.panels import PANELS, DATABASE_TYPES, TARGET_DB_RECOMMENDATIONS
from app.services.env_migration import extract_env_summary, detect_db_type_from_env


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
    return detect_db_type_from_env(text)


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


def get_pasarguard_env_summary() -> dict | None:
    if not PASARGUARD_ENV.exists():
        return None
    text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    return extract_env_summary(text)


def get_system_status() -> dict:
    """Server-wide detection for step 0 and install recheck."""
    pg = is_pasarguard_installed()
    marzban = is_marzban_installed()
    return {
        "pasarguard": pg,
        "marzban": marzban,
        "hiddify": is_hiddify_installed(),
        "docker": is_docker_running(),
        "root": is_root(),
        "pasarguard_db": get_pasarguard_db_type(),
        "marzban_db": get_marzban_db_type(),
        "pasarguard_path": str(PASARGUARD_DIR) if pg else None,
        "marzban_path": str(MARZBAN_DIR) if MARZBAN_DIR.exists() else None,
        "pasarguard_env": get_pasarguard_env_summary(),
    }


def _suggest_marzban_mode(marzban_installed: bool, pg_installed: bool) -> str:
    return "fresh"


def check_prerequisites(panel_id: str, marzban_mode: str | None = None, upload_id: str | None = None, upload_bundle_id: str | None = None) -> dict:
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

    upload_analysis = None
    bundle_status = None
    if upload_bundle_id:
        from app.services.upload_bundle import get_bundle_status
        bundle_status = get_bundle_status(upload_bundle_id)
        if bundle_status:
            upload_analysis = bundle_status.get("analysis")
    elif upload_id:
        from app.services.upload import get_upload_analysis
        upload_analysis = get_upload_analysis(upload_id)

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
            "id": "marzban_coexist",
            "label": {"en": "Marzban on server", "fa": "Marzban روی سرور", "ru": "Marzban на сервере"},
            "ok": True,
            "optional": True,
            "detail": {
                "en": f"Marzban still at {MARZBAN_DIR} — upload backup or use live SQLite if present",
                "fa": f"Marzban در {MARZBAN_DIR} — بکاپ آپلود کنید یا از SQLite زنده استفاده کنید",
                "ru": f"Marzban в {MARZBAN_DIR} — загрузите копию или используйте SQLite",
            },
        })

    if panel_id == "marzban":
        checks.append({
            "id": "pasarguard_installed",
            "label": {"en": "PasarGuard installed", "fa": "PasarGuard نصب شده", "ru": "PasarGuard установлен"},
            "ok": pg_installed,
            "detail": {
                "en": f"Found at {PASARGUARD_DIR}" if pg_installed else "Install PasarGuard manually before migration",
                "fa": "نصب شده" if pg_installed else "ابتدا PasarGuard را دستی نصب کنید",
                "ru": "Установлен" if pg_installed else "Установите PasarGuard вручную",
            },
        })
        has_marzban_data = marzban_installed or (MARZBAN_DATA / "db.sqlite3").exists()
        backup_ok = upload_analysis.get("backup_ok") if upload_analysis else False
        if bundle_status and bundle_status.get("complete"):
            backup_ok = True
        checks.append({
            "id": "marzban_source",
            "label": {"en": "Marzban backup", "fa": "بکاپ Marzban", "ru": "Копия Marzban"},
            "ok": has_marzban_data or backup_ok,
            "optional": not has_marzban_data and not backup_ok,
            "detail": {
                "en": (
                    f"Backup OK ({upload_analysis['total_files']} files)" if backup_ok and upload_analysis
                    else "Backup ready" if backup_ok
                    else "Live Marzban data found" if has_marzban_data
                    else "Upload backup in step 2"
                ),
                "fa": (
                    f"بکاپ تأیید شد ({upload_analysis['total_files']} فایل)" if backup_ok and upload_analysis
                    else "بکاپ آماده" if backup_ok
                    else "داده Marzban روی سرور" if has_marzban_data
                    else "در مرحله ۲ بکاپ آپلود کنید"
                ),
                "ru": (
                    f"Копия OK ({upload_analysis['total_files']} файлов)" if backup_ok and upload_analysis
                    else "Копия готова" if backup_ok
                    else "Данные Marzban на сервере" if has_marzban_data
                    else "Загрузите копию на шаге 2",
                ),
            },
        })
    elif panel_id == "3x-ui":
        checks.append({
            "id": "xui_db",
            "label": {"en": "3x-ui database", "fa": "دیتابیس 3x-ui", "ru": "База 3x-ui"},
            "ok": xui_db is not None,
            "optional": True,
            "detail": {
                "en": f"Found: {xui_db}" if xui_db else "Upload x-ui.db in step 2 (required before migration)",
                "fa": f"یافت شد: {xui_db}" if xui_db else "x-ui.db را در مرحله ۲ آپلود کنید",
                "ru": f"Найден: {xui_db}" if xui_db else "Загрузите x-ui.db на шаге 2",
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
            "upload_backup_ok": upload_analysis.get("backup_ok") if upload_analysis else None,
            "upload_source_db": upload_analysis.get("detected_source_db") if upload_analysis else None,
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
