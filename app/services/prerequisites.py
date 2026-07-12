"""System prerequisite checks."""

import os
import shutil
import subprocess
from pathlib import Path

from app.config import (
    PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA,
    MARZBAN_DIR, MARZBAN_DATA, XUI_DB_PATHS, HIDDIFY_DIR, HIDDIFY_MYSQL_PASS,
)
from app.panels import PANELS


def _run(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
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
        if "timescale" in text.lower():
            return "timescaledb"
        return "postgresql"
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


def check_prerequisites(panel_id: str) -> dict:
    panel = PANELS.get(panel_id)
    if not panel:
        return {"ok": False, "checks": [], "message_fa": "پنل نامعتبر"}

    checks = []

    # Root access
    root_ok = is_root()
    checks.append({
        "id": "root",
        "label_fa": "دسترسی root",
        "ok": root_ok,
        "detail_fa": "برای تغییر فایل‌ها و .env لازم است" if not root_ok else "فعال",
    })

    # Docker
    docker_ok = is_docker_running()
    checks.append({
        "id": "docker",
        "label_fa": "Docker",
        "ok": docker_ok,
        "detail_fa": "برای نصب/راه‌اندازی PasarGuard لازم است" if not docker_ok else "در حال اجرا",
    })

    pg_installed = is_pasarguard_installed()
    marzban_installed = is_marzban_installed()
    hiddify_installed = is_hiddify_installed()
    xui_db = find_xui_db()

    if panel.requires_pasarguard:
        checks.append({
            "id": "pasarguard",
            "label_fa": "PasarGuard نصب شده",
            "ok": pg_installed,
            "detail_fa": (
                "PasarGuard باید نصب باشد — از wizard نصب کنید"
                if not pg_installed else f"نصب در {PASARGUARD_DIR}"
            ),
        })

    if panel_id == "marzban":
        checks.append({
            "id": "marzban",
            "label_fa": "Marzban نصب شده (یا بکاپ)",
            "ok": marzban_installed or True,  # backup upload is alternative
            "detail_fa": (
                f"Marzban در {MARZBAN_DIR} یافت شد"
                if marzban_installed
                else "Marzban یافت نشد — می‌توانید فایل بکاپ آپلود کنید"
            ),
            "optional": not marzban_installed,
        })
        if pg_installed and marzban_installed:
            checks.append({
                "id": "conflict",
                "label_fa": "تداخل نصب",
                "ok": False,
                "detail_fa": "هر دو Marzban و PasarGuard نصب هستند — از روش آپلود بکاپ استفاده کنید",
            })

    if panel_id == "3x-ui":
        checks.append({
            "id": "xui_db",
            "label_fa": "دیتابیس 3x-ui",
            "ok": xui_db is not None,
            "detail_fa": (
                f"یافت شد: {xui_db}" if xui_db else "یافت نشد — فایل x-ui.db را آپلود کنید"
            ),
            "optional": xui_db is None,
        })

    if panel_id == "hiddify":
        checks.append({
            "id": "hiddify",
            "label_fa": "Hiddify Manager",
            "ok": hiddify_installed,
            "detail_fa": (
                f"نصب در {HIDDIFY_DIR}" if hiddify_installed
                else "یافت نشد — dump دیتابیس MySQL را آپلود کنید"
            ),
            "optional": not hiddify_installed,
        })

    # Determine overall status
    required_failed = [c for c in checks if not c.get("optional") and not c["ok"]]
    ok = len(required_failed) == 0

    if ok:
        msg = "همه پیش‌نیازها برآورده شده — آماده مهاجرت"
    else:
        failed = ", ".join(c["label_fa"] for c in required_failed)
        msg = f"پیش‌نیازهای ناقص: {failed}"

    return {"ok": ok, "checks": checks, "message_fa": msg, "detected": {
        "pasarguard": pg_installed,
        "marzban": marzban_installed,
        "hiddify": hiddify_installed,
        "xui_db": str(xui_db) if xui_db else None,
        "pasarguard_db": get_pasarguard_db_type(),
        "marzban_db": get_marzban_db_type(),
    }}


def get_recommended_target_dbs(source_panel: str, source_db: str) -> list[dict]:
    from app.panels import TARGET_DB_RECOMMENDATIONS, DATABASE_TYPES

    recs = TARGET_DB_RECOMMENDATIONS.get(source_db, ["sqlite", "timescaledb"])
    result = []
    for i, db_id in enumerate(recs):
        info = DATABASE_TYPES.get(db_id, {})
        result.append({
            "id": db_id,
            "name": info.get("name", db_id),
            "name_fa": info.get("name_fa", db_id),
            "recommended": i == 0,
            "reason_fa": _recommendation_reason(source_panel, source_db, db_id, i == 0),
        })
    return result


def _recommendation_reason(panel: str, source: str, target: str, is_first: bool) -> str:
    if panel == "marzban" and source == target:
        return "ساده‌ترین مسیر — همان نوع دیتابیس، کمترین ریسک"
    if target == "timescaledb":
        return "توصیه PasarGuard برای پروداکشن — آمار و لاگ بهتر"
    if is_first:
        return "سازگارترین گزینه با دیتابیس مبدأ"
    return "گزینه جایگزین"
