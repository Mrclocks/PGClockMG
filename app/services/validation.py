"""Pre-migration validation — block migration if prerequisites fail."""

from pathlib import Path

from app.config import (
    MARZBAN_DIR, MARZBAN_DATA, PASARGUARD_DIR, PASARGUARD_ENV, PASARGUARD_DATA, TOOLS_DIR,
)
from app.panels import PANELS
from app.services.prerequisites import (
    check_prerequisites,
    is_docker_running,
    is_root,
    is_pasarguard_installed,
    is_marzban_installed,
    find_xui_db,
    get_marzban_db_type,
)
from app.services.upload import get_upload_path, get_upload_analysis
from app.services.upload_bundle import get_bundle_status
from app.services.upload_requirements import get_upload_requirements


def validate_migration(params: dict) -> dict:
    """Return {ok: bool, errors: [{en, fa, ru}]}"""
    errors: list[dict] = []
    panel_id = params.get("source_panel")
    panel = PANELS.get(panel_id)
    if not panel:
        return {"ok": False, "errors": [_msg("Invalid panel", "پنل نامعتبر", "Неверная панель")]}

    if not is_root():
        errors.append(_msg("Root access required", "دسترسی root لازم است", "Требуется root"))
    if not is_docker_running():
        errors.append(_msg("Docker must be running", "Docker باید در حال اجرا باشد", "Docker должен работать"))

    marzban_mode = params.get("marzban_mode") or "auto"
    bundle_id = params.get("upload_bundle_id")
    prereq = check_prerequisites(panel_id, marzban_mode=marzban_mode, upload_bundle_id=bundle_id)
    if not prereq["ok"]:
        for c in prereq["checks"]:
            if not c.get("optional") and not c["ok"]:
                errors.append(c["label"])

    source_db = params.get("source_db")
    target_db = params.get("target_db")
    upload_path = params.get("upload_path") or params.get("upload_work_dir")
    if not upload_path and params.get("upload_id"):
        upload_path = get_upload_path(params["upload_id"])

    bundle_status = get_bundle_status(bundle_id) if bundle_id else None
    upload_analysis = (
        params.get("upload_analysis")
        or (bundle_status.get("analysis") if bundle_status else None)
        or (get_upload_analysis(params["upload_id"]) if params.get("upload_id") else None)
    )

    errors.extend(_validate_uploads(panel_id, source_db, marzban_mode, upload_path, bundle_status, upload_analysis))

    if panel_id == "marzban":
        errors.extend(_validate_marzban(params, marzban_mode, source_db, target_db, upload_path, upload_analysis, bundle_status))
    elif panel_id == "3x-ui":
        errors.extend(_validate_xui(upload_path, bundle_status))
    elif panel_id == "remnawave":
        if not params.get("remnawave_url") or not params.get("remnawave_token"):
            errors.append(_msg("Remnawave URL and API token required", "URL و Token رمناوی لازم است", "Нужны URL и токен Remnawave"))
    elif panel_id == "pasarguard":
        if not (PASARGUARD_DATA / "db.sqlite3").exists() and not upload_path:
            errors.append(_msg("PasarGuard database or backup upload required", "دیتابیس یا بکاپ PasarGuard لازم است", "Нужна БД или копия PasarGuard"))

    if source_db != target_db:
        db_migrations = TOOLS_DIR / "db-migrations"
        if not db_migrations.exists():
            errors.append(_msg("db-migrations tool missing — re-run install.sh", "ابزار db-migrations نیست — install.sh را اجرا کنید", "Нет db-migrations — переустановите"))

    if source_db in ("mysql", "mariadb", "postgresql", "timescaledb"):
        pwd = params.get("source_db_password") or params.get("target_db_password")
        if not pwd and panel_id == "marzban" and marzban_mode == "inplace":
            env_path = (PASARGUARD_DIR if PASARGUARD_DIR.exists() else MARZBAN_DIR) / ".env"
            if env_path.exists():
                from app.services.env_migration import read_env_var
                text = env_path.read_text(encoding="utf-8", errors="ignore")
                pwd = read_env_var(text, "MYSQL_ROOT_PASSWORD") or read_env_var(text, "MYSQL_PASSWORD")
        if not pwd and upload_analysis and upload_analysis.get("mysql_password_found"):
            pwd = "from-env"
        if not pwd and bundle_status:
            for s in bundle_status.get("slots", []):
                if s.get("id") == "env" and s.get("ok"):
                    pwd = "from-env"
        if not pwd:
            errors.append(_msg("Database password required", "رمز دیتابیس لازم است", "Нужен пароль БД"))

    return {"ok": len(errors) == 0, "errors": errors}


def _validate_uploads(panel_id, source_db, marzban_mode, upload_path, bundle_status, upload_analysis) -> list:
    errors = []
    reqs = get_upload_requirements(panel_id, source_db, marzban_mode)
    if reqs["upload_mode"] == "none":
        return errors
    if reqs["upload_mode"] == "optional":
        if upload_path or (bundle_status and bundle_status.get("complete")):
            pass
        else:
            return errors
    if reqs["upload_mode"] == "required":
        if bundle_status and bundle_status.get("complete"):
            return errors
        if upload_path and upload_analysis and upload_analysis.get("backup_ok"):
            return errors
        if panel_id == "marzban" and marzban_mode == "fresh" and is_marzban_installed():
            return errors
        if panel_id == "3x-ui" and find_xui_db():
            return errors
        if bundle_status and bundle_status.get("missing"):
            for m in bundle_status["missing"]:
                label = m.get("label")
                if label:
                    errors.append(label)
            return errors
        errors.append(_msg(
            "Upload all required files before continuing",
            "فایل‌های اجباری را آپلود کنید",
            "Загрузите все обязательные файлы",
        ))
    return errors


def _validate_marzban(params, mode, source_db, target_db, upload_path, upload_analysis=None, bundle_status=None) -> list:
    errors = []
    if mode == "auto":
        if upload_path or bundle_status:
            mode = "fresh"
        elif is_marzban_installed() and not is_pasarguard_installed():
            mode = "inplace"
        else:
            mode = "fresh"

    if mode == "inplace":
        if not is_marzban_installed():
            errors.append(_msg("Marzban must be installed for in-place migration", "Marzban باید نصب باشد (روش درجا)", "Marzban должен быть установлен"))
        if is_pasarguard_installed():
            errors.append(_msg("PasarGuard must NOT exist for in-place mode", "PasarGuard نباید نصب باشد (روش درجا)", "PasarGuard НЕ должен быть установлен"))
        if not MARZBAN_DIR.exists() and not (MARZBAN_DATA / "db.sqlite3").exists():
            errors.append(_msg("Marzban data not found at /opt/marzban or /var/lib/marzban", "داده Marzban یافت نشد", "Данные Marzban не найдены"))
        detected = get_marzban_db_type()
        if detected and source_db and detected != source_db:
            errors.append(_msg(
                f"Source DB mismatch: server has {detected}, you selected {source_db}",
                f"نوع DB سرور ({detected}) با انتخاب شما ({source_db}) فرق دارد",
                f"Несовпадение БД: на сервере {detected}, выбрано {source_db}",
            ))
        if source_db == "sqlite" and not (MARZBAN_DATA / "db.sqlite3").exists() and not (PASARGUARD_DATA / "db.sqlite3").exists():
            errors.append(_msg("db.sqlite3 not found", "فایل db.sqlite3 یافت نشد", "db.sqlite3 не найден"))

    elif mode == "fresh":
        if not is_pasarguard_installed():
            errors.append(_msg("Install PasarGuard manually before fresh migration", "ابتدا PasarGuard را دستی نصب کنید", "Установите PasarGuard вручную"))
        has_source = bool(upload_path) or (bundle_status and bundle_status.get("complete"))
        has_source = has_source or (is_marzban_installed() and source_db == "sqlite" and (MARZBAN_DATA / "db.sqlite3").exists())
        if source_db in ("mysql", "mariadb"):
            has_source = has_source or is_marzban_installed()
        if upload_analysis:
            if not upload_analysis.get("backup_ok") and not (bundle_status and bundle_status.get("complete")):
                for m in upload_analysis.get("missing", []):
                    if isinstance(m, dict):
                        errors.append(m)
            elif upload_analysis.get("detected_source_db") and source_db:
                if upload_analysis["detected_source_db"] != source_db:
                    errors.append(_msg(
                        f"Backup contains {upload_analysis['detected_source_db']} but you selected {source_db}",
                        f"بکاپ {upload_analysis['detected_source_db']} است ولی شما {source_db} انتخاب کردید",
                        f"В копии {upload_analysis['detected_source_db']}, выбрано {source_db}",
                    ))
        elif not has_source:
            errors.append(_msg("Marzban backup or live database required", "بکاپ یا دیتابیس Marzban لازم است", "Нужна копия или БД Marzban"))

    return errors


def _validate_xui(upload_path, bundle_status=None) -> list:
    errors = []
    if not is_pasarguard_installed():
        errors.append(_msg("PasarGuard must be installed first", "ابتدا PasarGuard را نصب کنید", "Сначала установите PasarGuard"))
    has_db = find_xui_db() or upload_path or (bundle_status and bundle_status.get("complete"))
    if not has_db:
        errors.append(_msg("x-ui.db not found — upload backup", "x-ui.db یافت نشد — بکاپ آپلود کنید", "x-ui.db не найден — загрузите копию"))
    return errors


def _msg(en: str, fa: str, ru: str) -> dict:
    return {"en": en, "fa": fa, "ru": ru}
