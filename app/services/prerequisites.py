"""System prerequisite checks."""

import os
import shutil
import subprocess
from pathlib import Path

from app.config import (
    PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA, UPLOAD_DIR, BACKUP_DIR,
    MARZBAN_DIR, MARZBAN_DATA, XUI_DB_PATHS, HIDDIFY_DIR,
)
from app.panels import PANELS, DATABASE_TYPES, TARGET_DB_RECOMMENDATIONS
from app.services.env_migration import extract_env_summary, detect_db_type_from_env, extract_env_password_candidates


def _run(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _read_mem_value_bytes(label: str) -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(label):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
    except Exception:
        return None
    return None


def _disk_stats(path: Path) -> dict | None:
    try:
        usage = shutil.disk_usage(str(path))
    except Exception:
        return None
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def _classify_resources(resources: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    memory = resources.get("memory") or {}
    storage = resources.get("storage") or {}
    upload = storage.get("upload") or {}
    backup = storage.get("backup") or {}

    mem_avail = memory.get("available_bytes") or 0
    mem_total = memory.get("total_bytes") or 0
    upload_free = upload.get("free_bytes") or 0
    backup_free = backup.get("free_bytes") or 0
    cpu_count = resources.get("cpu_count") or 0
    load_ratio = resources.get("load_ratio_1m")

    # 2 GB RAM + 2 CPU is considered a normal baseline for this panel family.
    if mem_avail and mem_avail < 768 * 1024 * 1024:
        reasons.append("low_ram")
    if upload_free and upload_free < 4 * 1024 * 1024 * 1024:
        reasons.append("low_upload_disk")
    if backup_free and backup_free < 4 * 1024 * 1024 * 1024:
        reasons.append("low_backup_disk")
    if cpu_count and cpu_count <= 1:
        reasons.append("low_cpu")
    if isinstance(load_ratio, (int, float)) and load_ratio >= 1.6:
        reasons.append("high_load")

    if reasons:
        return "weak", reasons

    strong_signals = 0
    if mem_total >= 8 * 1024 * 1024 * 1024 and mem_avail >= 3 * 1024 * 1024 * 1024:
        strong_signals += 1
    if upload_free >= 16 * 1024 * 1024 * 1024 and backup_free >= 16 * 1024 * 1024 * 1024:
        strong_signals += 1
    if cpu_count >= 4:
        strong_signals += 1
    if isinstance(load_ratio, (int, float)) and load_ratio <= 0.65:
        strong_signals += 1

    if strong_signals >= 3:
        return "strong", []
    return "normal", []


def _resource_status() -> dict:
    load_average = None
    load_ratio = None
    try:
        one, five, fifteen = os.getloadavg()
        load_average = {"1m": one, "5m": five, "15m": fifteen}
    except Exception:
        pass

    cpu_count = os.cpu_count() or 0
    if load_average and cpu_count:
        load_ratio = round(load_average["1m"] / cpu_count, 2)

    resources = {
        "cpu_count": cpu_count or None,
        "load_average": load_average,
        "load_ratio_1m": load_ratio,
        "memory": {
            "total_bytes": _read_mem_value_bytes("MemTotal:"),
            "available_bytes": _read_mem_value_bytes("MemAvailable:"),
        },
        "storage": {
            "upload": _disk_stats(UPLOAD_DIR),
            "backup": _disk_stats(BACKUP_DIR),
        },
    }
    profile, reasons = _classify_resources(resources)
    resources["profile"] = profile
    resources["profile_reasons"] = reasons
    return resources


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
    # Prefer a cheap probe — full `docker info` can stall several seconds under load.
    ok, _ = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=2)
    if ok:
        return True
    ok, _ = _run(["docker", "info", "-f", "{{.ServerVersion}}"], timeout=3)
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
    text = env_path.read_text(encoding="utf-8", errors="ignore").lower()
    if "mariadb" in text:
        return "mariadb"
    if "mysql" in text or "pymysql" in text:
        return "mysql"
    if "sqlite" in text:
        return "sqlite"
    return None


def get_pasarguard_env_summary() -> dict | None:
    if not PASARGUARD_ENV.exists():
        return None
    text = PASARGUARD_ENV.read_text(encoding="utf-8", errors="ignore")
    return extract_env_summary(text)


def _password_candidates_from_env(path: Path, db_type: str | None) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    return extract_env_password_candidates(text, db_type)


def get_system_status() -> dict:
    """Server-wide detection for step 0 and install recheck."""
    pg = is_pasarguard_installed()
    marzban = is_marzban_installed()
    pg_db = get_pasarguard_db_type()
    mz_db = get_marzban_db_type()
    return {
        "pasarguard": pg,
        "marzban": marzban,
        "hiddify": is_hiddify_installed(),
        "docker": is_docker_running(),
        "root": is_root(),
        "pasarguard_db": pg_db,
        "marzban_db": mz_db,
        "pasarguard_path": str(PASARGUARD_DIR) if pg else None,
        "marzban_path": str(MARZBAN_DIR) if MARZBAN_DIR.exists() else None,
        "pasarguard_env": get_pasarguard_env_summary(),
        "pasarguard_password_candidates": _password_candidates_from_env(PASARGUARD_ENV, pg_db) if pg else [],
        "marzban_env": extract_env_summary(
            (MARZBAN_DIR / ".env").read_text(encoding="utf-8", errors="ignore")
        ) if marzban and (MARZBAN_DIR / ".env").exists() else None,
        "marzban_password_candidates": _password_candidates_from_env(MARZBAN_DIR / ".env", mz_db) if marzban else [],
        "resources": _resource_status(),
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
                    else "Загрузите копию на шаге 2"
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
                "en": f"At {HIDDIFY_DIR}" if hiddify_installed else "Upload Hiddify JSON Export in next step",
                "fa": "یا بکاپ JSON آپلود کنید" if not hiddify_installed else f"در {HIDDIFY_DIR}",
                "ru": "Загрузите JSON Export" if not hiddify_installed else "Найден",
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
