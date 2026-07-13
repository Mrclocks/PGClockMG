"""Analyze uploaded backup archives (Marzban, x-ui, etc.)."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.env_migration import (
    read_env_var,
    transform_marzban_env,
    transform_xray_config,
    detect_db_type_from_env,
    extract_env_summary,
    extract_env_password_candidates,
)

CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("database_sqlite", ("db.sqlite3", "marzban.db")),
    ("database_sql", (".sql",)),
    ("config_env", (".env",)),
    ("config_compose", ("docker-compose.yml", "docker-compose.yaml")),
    ("config_xray", ("xray_config.json",)),
    ("ssl_certs", ("fullchain.pem", "key.pem", "cert.pem")),
    ("templates", ()),
]

MARZBAN_PATH_MARKERS = (
    "/var/lib/marzban",
    "/opt/marzban",
    "marzban",
    "v2ray/",
    "V2RAY_SUBSCRIPTION",
)


def get_upload_dir(upload_id: str, base: Path) -> Path | None:
    d = base / upload_id
    return d if d.exists() else None


def resolve_extract_root(upload_dir: Path) -> Path:
    """Best directory to search for backup files (handles nested zip layouts)."""
    candidates: list[tuple[int, Path]] = []

    search_roots = []
    extracted = upload_dir / "extracted"
    if extracted.exists():
        search_roots.append(extracted)
    search_roots.append(upload_dir)

    for root in search_roots:
        score = _score_directory(root)
        if score > 0:
            candidates.append((score, root))

        for sub in root.rglob("*"):
            if not sub.is_dir():
                continue
            if sub.name in ("marzban", "pasarguard", "xray", "v2ray", "certs", "templates"):
                s = _score_directory(sub)
                if s > 0:
                    candidates.append((s, sub))
            if sub.parts[-3:] == ("var", "lib", "marzban") or sub.parts[-2:] == ("lib", "marzban"):
                s = _score_directory(sub)
                if s > 0:
                    candidates.append((s + 5, sub))

    if not candidates:
        return extracted if extracted.exists() else upload_dir

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _score_directory(path: Path) -> int:
    score = 0
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name in ("db.sqlite3", "marzban.db"):
            score += 10
        if name.endswith(".sql"):
            score += 8
        if name == ".env":
            score += 6
        if name == "xray_config.json":
            score += 4
        if name in ("docker-compose.yml", "docker-compose.yaml"):
            score += 3
    return score


def _categorize_file(path: Path) -> str:
    name = path.name.lower()
    if name in ("db.sqlite3", "marzban.db", "x-ui.db"):
        return "database_sqlite"
    if name.endswith(".sql"):
        return "database_sql"
    if name == ".env":
        return "config_env"
    if name in ("docker-compose.yml", "docker-compose.yaml"):
        return "config_compose"
    if name == "xray_config.json":
        return "config_xray"
    if "certs" in path.parts and name.endswith((".pem", ".crt", ".key")):
        return "ssl_certs"
    if "templates" in path.parts:
        return "templates"
    if "v2ray" in path.parts or "xray" in path.parts:
        return "templates"
    return "other"


def detect_db_from_env(text: str) -> str | None:
    return detect_db_type_from_env(text)


def analyze_upload_directory(upload_dir: Path) -> dict:
    root = resolve_extract_root(upload_dir)
    inventory: list[dict] = []
    categories: dict[str, int] = {}
    paths: dict[str, str | None] = {
        "sqlite": None,
        "sql": None,
        "env": None,
        "compose": None,
        "xray_config": None,
        "data_dir": None,
    }

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(upload_dir)).replace("\\", "/")
        cat = _categorize_file(p)
        categories[cat] = categories.get(cat, 0) + 1
        inventory.append({
            "path": rel,
            "name": p.name,
            "category": cat,
            "size": p.stat().st_size,
            "pasarguard_note": _pasarguard_note(p, cat),
        })

        name = p.name.lower()
        if name in ("db.sqlite3", "marzban.db") and not paths["sqlite"]:
            paths["sqlite"] = str(p)
            paths["data_dir"] = str(p.parent)
        if name.endswith(".sql") and not paths["sql"]:
            paths["sql"] = str(p)
        if name == ".env" and not paths["env"]:
            paths["env"] = str(p)
        if name in ("docker-compose.yml", "docker-compose.yaml") and not paths["compose"]:
            paths["compose"] = str(p)
        if name == "xray_config.json" and not paths["xray_config"]:
            paths["xray_config"] = str(p)
            if not paths["data_dir"]:
                paths["data_dir"] = str(p.parent)

    # Also scan upload_dir for single-file uploads (sql/sqlite at top level)
    for p in upload_dir.iterdir():
        if not p.is_file():
            continue
        if p.name.lower().endswith(".sql") and not paths["sql"]:
            paths["sql"] = str(p)
        if p.name.lower() in ("db.sqlite3", "marzban.db", "x-ui.db") and not paths["sqlite"]:
            paths["sqlite"] = str(p)

    panel_hint = _detect_panel(inventory, paths)
    env_text = ""
    if paths["env"]:
        env_text = Path(paths["env"]).read_text(encoding="utf-8", errors="ignore")

    detected_source_db = detect_db_from_env(env_text) if env_text else None
    if not detected_source_db:
        if paths["sqlite"]:
            detected_source_db = "sqlite"
        elif paths["sql"]:
            low = paths["sql"].lower()
            detected_source_db = "mariadb" if "mariadb" in low else "mysql"

    env_summary = extract_env_summary(env_text) if env_text else None
    password_candidates = (
        extract_env_password_candidates(env_text, detected_source_db) if env_text else []
    )

    env_mapping: list[dict] = []
    if env_text and panel_hint == "marzban":
        target = detected_source_db or "sqlite"
        transformed = transform_marzban_env(env_text, target, env_summary.get("db_password") if env_summary else None)
        env_mapping = _diff_env_paths(env_text, transformed)

    missing: list[dict] = []
    backup_ok = False
    if panel_hint == "marzban":
        if detected_source_db == "sqlite":
            backup_ok = paths["sqlite"] is not None
            if not backup_ok:
                missing.append(_msg("db.sqlite3 not found in zip", "db.sqlite3 در zip نیست", "db.sqlite3 не найден"))
        elif detected_source_db in ("mysql", "mariadb"):
            backup_ok = paths["sql"] is not None
            if not backup_ok:
                missing.append(_msg(".sql dump not found in zip", "فایل .sql در zip نیست", "Файл .sql не найден"))
            if not password_candidates:
                missing.append(_msg(
                    "MYSQL_ROOT_PASSWORD / DB_PASSWORD not in backup .env — enter manually",
                    "رمز MySQL در .env بکاپ نیست — دستی وارد کنید",
                    "Пароль MySQL не в .env — введите вручную",
                ))
        else:
            backup_ok = bool(paths["sqlite"] or paths["sql"])
    elif panel_hint == "3x-ui":
        backup_ok = paths["sqlite"] is not None and "x-ui" in (paths["sqlite"] or "").lower()
    else:
        backup_ok = bool(paths["sqlite"] or paths["sql"])

    warnings: list[dict] = []
    if panel_hint == "marzban" and backup_ok and not paths["env"]:
        warnings.append(_msg(
            "No .env in backup — subscription/SSL settings may need manual setup",
            "فایل .env در بکاپ نیست — تنظیمات اشتراک/SSL ممکن است دستی لازم باشد",
            "Нет .env в копии — настройки вручную",
        ))

    return {
        "extract_root": str(root.relative_to(upload_dir)).replace("\\", "/") if root != upload_dir else ".",
        "total_files": len(inventory),
        "categories": categories,
        "inventory": inventory[:200],
        "inventory_truncated": len(inventory) > 200,
        "paths": {k: (str(Path(v).relative_to(upload_dir)).replace("\\", "/") if v else None) for k, v in paths.items()},
        "panel_hint": panel_hint,
        "detected_source_db": detected_source_db,
        "mysql_password_found": bool(password_candidates),
        "env_summary": env_summary,
        "password_candidates": password_candidates,
        "env_mapping": env_mapping[:30],
        "backup_ok": backup_ok,
        "missing": missing,
        "warnings": warnings,
        "has_certs": categories.get("ssl_certs", 0) > 0,
        "has_templates": categories.get("templates", 0) > 0,
        "has_xray_config": paths["xray_config"] is not None,
    }


def _detect_panel(inventory: list[dict], paths: dict) -> str | None:
    sqlite_path = paths.get("sqlite") or ""
    if "x-ui" in sqlite_path.lower():
        return "3x-ui"
    for item in inventory:
        p = item["path"].lower()
        if "hiddify" in p:
            return "hiddify"
        if "marzban" in p or item["name"] in ("db.sqlite3", "marzban.db"):
            return "marzban"
    if paths.get("sqlite") or paths.get("sql"):
        return "marzban"
    return None


def _pasarguard_note(path: Path, category: str) -> str | None:
    rel = str(path).replace("\\", "/")
    if category == "database_sqlite":
        return "/var/lib/pasarguard/db.sqlite3"
    if category == "config_env":
        return "Marzban .env → PasarGuard .env (paths & drivers)"
    if category == "config_xray":
        return "/var/lib/pasarguard/xray_config.json"
    if category == "config_compose":
        return "/opt/pasarguard/docker-compose.yml"
    if category == "ssl_certs":
        return rel.replace("/var/lib/marzban/", "/var/lib/pasarguard/").replace("marzban", "pasarguard")
    if category == "templates" and "v2ray" in rel:
        return rel.replace("v2ray", "xray").replace("marzban", "pasarguard")
    if "marzban" in rel:
        return rel.replace("/var/lib/marzban", "/var/lib/pasarguard").replace("/opt/marzban", "/opt/pasarguard")
    return None


def _diff_env_paths(old: str, new: str) -> list[dict]:
    mapping = []
    for marker in MARZBAN_PATH_MARKERS:
        if marker.lower() in old.lower():
            mapping.append({
                "from": marker,
                "to": marker.replace("marzban", "pasarguard").replace("v2ray", "xray").replace("V2RAY", "XRAY"),
            })
    if "V2RAY_SUBSCRIPTION_TEMPLATE" in old.upper():
        mapping.append({"from": "V2RAY_SUBSCRIPTION_TEMPLATE", "to": "XRAY_SUBSCRIPTION_TEMPLATE"})
    if "sqlite://" in old and "aiosqlite" in new:
        mapping.append({"from": "sqlite:// (sync)", "to": "sqlite+aiosqlite:// (async)"})
    if "pymysql" in old.lower() and "asyncmy" in new.lower():
        mapping.append({"from": "mysql+pymysql", "to": "mysql+asyncmy"})
    return mapping


def find_file_in_upload(upload_dir: Path, names: tuple[str, ...]) -> Path | None:
    root = resolve_extract_root(upload_dir)
    for name in names:
        for p in root.rglob(name):
            if p.is_file():
                return p
    for p in upload_dir.iterdir():
        if p.is_file() and p.name.lower() in {n.lower() for n in names}:
            return p
    return None


def _msg(en: str, fa: str, ru: str) -> dict:
    return {"en": en, "fa": fa, "ru": ru}
