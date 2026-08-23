"""PasarGuard in-container import for Hiddify users.

The panel image keeps application code under ``/code``. Running
``python /var/lib/pasarguard/*.py`` puts the script dir first on
``sys.path`` and yields ``No module named 'app'``. This module:

1. Builds an import script that bootstraps ``/code`` onto ``sys.path``
2. Runs it via ``compose exec -w /code`` and a ``docker run`` fallback
3. Always creates/uses group ``hiddify-test`` and assigns every user to it
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import PASARGUARD_DATA, PASARGUARD_DIR, PASARGUARD_ENV
from app.services.pasarguard_ops import (
    PASARGUARD_SERVICE_CANDIDATES,
    resolve_pasarguard_image,
    resolve_pasarguard_service,
    write_docker_env_file,
)

# Dedicated group for migrated Hiddify users (PasarGuard requires ≥1 group).
HIDDIFY_TEST_GROUP = "hiddify-test"
HIDDIFY_TEST_INBOUND_TAG = "hiddify-test"

# Executed inside the PasarGuard panel image (any DB engine).
IMPORT_SCRIPT = r'''
import asyncio
import hmac
import json
import sys
import traceback
from base64 import b64encode
from datetime import datetime, timezone
from hashlib import sha256
from math import ceil
from pathlib import Path
from time import time
from uuid import UUID

# CRITICAL: script lives under /var/lib/pasarguard — force panel code onto path.
for _p in ("/code", "/app", "/usr/src/app"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

payload_path = Path(sys.argv[1])
result_path = Path(sys.argv[2])
GROUP_NAME = "hiddify-test"
GROUP_TAG = "hiddify-test"

def write_result(payload):
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

try:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
except Exception as e:
    write_result({
        "ok": False,
        "error": f"bad payload: {e}",
        "traceback": traceback.format_exc(),
        "created": [],
        "errors": [{"username": "*", "error": f"bad payload: {e}"}],
        "skipped": [],
    })
    raise SystemExit(0)

users = payload.get("users") or []


async def find_user_by_uuid(db, uid: UUID, uuid_index=None):
    """Return existing user whose VLESS/VMess proxy id matches ``uid``."""
    target = str(uid).lower()
    if isinstance(uuid_index, dict) and target in uuid_index:
        return uuid_index[target]

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import User

    try:
        rows = await db.execute(
            select(User).options(selectinload(User.groups)).limit(5000)
        )
        users = list(rows.unique().scalars().all())
    except Exception:
        try:
            rows = await db.execute(select(User).limit(5000))
            users = list(rows.scalars().all())
        except Exception:
            return None

    for user in users:
        settings = getattr(user, "proxy_settings", None) or {}
        candidates = []
        if isinstance(settings, dict):
            for key in ("vless", "vmess"):
                block = settings.get(key) or {}
                if isinstance(block, dict):
                    candidates.append(str(block.get("id") or "").strip().lower())
            for block in settings.values():
                if isinstance(block, dict):
                    candidates.append(str(block.get("id") or "").strip().lower())
        for attr in ("vless", "vmess"):
            block = getattr(settings, attr, None)
            if block is None:
                continue
            got = str(getattr(block, "id", "") or "").strip().lower()
            if not got and isinstance(block, dict):
                got = str(block.get("id") or "").strip().lower()
            candidates.append(got)
        if target in candidates:
            return user
    return None


async def build_uuid_index(db):
    """Map lowercase proxy UUID → user for fast re-import dedup."""
    from sqlalchemy import select

    from app.db.models import User

    index = {}
    try:
        rows = await db.execute(select(User).limit(10000))
        users = list(rows.scalars().all())
    except Exception:
        return index
    for user in users:
        settings = getattr(user, "proxy_settings", None) or {}
        ids = []
        if isinstance(settings, dict):
            for key in ("vless", "vmess"):
                block = settings.get(key) or {}
                if isinstance(block, dict) and block.get("id"):
                    ids.append(str(block.get("id")).strip().lower())
        for attr in ("vless", "vmess"):
            block = getattr(settings, attr, None)
            if block is None:
                continue
            got = getattr(block, "id", None)
            if got is None and isinstance(block, dict):
                got = block.get("id")
            if got:
                ids.append(str(got).strip().lower())
        for got in ids:
            if got and got not in index:
                index[got] = user
    return index



async def make_sub_token(db, user_id: int) -> str:
    from app.db.crud.general import get_jwt_secret_key

    secret = await get_jwt_secret_key(db)
    if not secret:
        raise RuntimeError("PasarGuard JWT secret is missing")
    data = "v3," + str(int(user_id)) + "," + str(ceil(time()))
    data_b64 = b64encode(data.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")
    signature = (
        b64encode(
            hmac.new(secret.encode("utf-8"), data_b64.encode("utf-8"), sha256).digest(),
            altchars=b"-_",
        )
        .decode("utf-8")
        .rstrip("=")
    )
    return data_b64 + "." + signature


async def ensure_hiddify_test_group(db):
    """Find or create the dedicated hiddify-test group; return [group]."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.crud.group import create_group, get_group
    from app.db.models import Group
    from app.models.group import GroupCreate, GroupListQuery

    groups = []
    try:
        groups, _total = await get_group(db, GroupListQuery(limit=200))
        groups = list(groups or [])
    except TypeError:
        try:
            groups, _total = await get_group(db, 0, 200)
            groups = list(groups or [])
        except Exception:
            groups = []
    except Exception:
        groups = []

    if not groups:
        try:
            rows = await db.execute(
                select(Group).options(selectinload(Group.inbounds)).limit(200)
            )
            groups = list(rows.unique().scalars().all())
        except Exception:
            groups = []

    for g in groups:
        if str(getattr(g, "name", "") or "").strip().lower() == GROUP_NAME:
            return [g]

    try:
        g = await create_group(
            db,
            GroupCreate(name=GROUP_NAME, inbound_tags=[GROUP_TAG]),
        )
        return [g]
    except Exception as e:
        # Race / duplicate name — re-list and pick by name
        try:
            groups, _total = await get_group(db, GroupListQuery(limit=200))
            groups = list(groups or [])
        except Exception:
            pass
        for g in groups or []:
            if str(getattr(g, "name", "") or "").strip().lower() == GROUP_NAME:
                return [g]
        raise RuntimeError(
            f"Could not create group {GROUP_NAME!r}: {e}. "
            "Create it in the panel, then retry."
        ) from e


async def main():
    # Import AFTER sys.path bootstrap
    from app.db import GetDB
    from app.db.crud.admin import get_owner
    from app.db.crud.user import create_user, get_user
    from app.db.models import UserStatus
    from app.models.proxy import ProxyTable, VlessSettings, VMessSettings, TrojanSettings
    from app.models.user import UserCreate

    created = []
    errors = []
    skipped = []
    group_id = None
    group_name = GROUP_NAME

    async with GetDB() as db:
        owner = await get_owner(db)
        if owner is None:
            write_result({
                "ok": False,
                "error": "No PasarGuard owner found — create owner first (pasarguard cli generate-temp-key)",
                "created": [],
                "errors": [{"username": "*", "error": "no owner"}],
                "skipped": [],
            })
            return

        try:
            groups = await ensure_hiddify_test_group(db)
        except Exception as e:
            write_result({
                "ok": False,
                "error": str(e)[:500],
                "traceback": traceback.format_exc(),
                "created": [],
                "errors": [{"username": "*", "error": str(e)[:300]}],
                "skipped": [],
            })
            return

        group = groups[0]
        group_id = int(group.id)
        group_name = str(getattr(group, "name", GROUP_NAME) or GROUP_NAME)
        group_ids = [group_id]

        try:
            uuid_index = await build_uuid_index(db)
        except Exception:
            uuid_index = {}

        for row in users:
            username = (row.get("username") or "").strip()
            uuid_s = (row.get("uuid") or "").strip()
            if not username or not uuid_s:
                skipped.append({"username": username, "reason": "missing username/uuid"})
                continue
            try:
                uid = UUID(uuid_s)
            except Exception as e:
                errors.append({"username": username, "error": f"bad uuid: {e}"})
                continue

            existing = None
            # Prefer UUID match so re-runs keep the same PasarGuard user + old links
            try:
                existing = await find_user_by_uuid(db, uid, uuid_index)
            except Exception:
                existing = None

            if existing is None:
                try:
                    existing = await get_user(
                        db, username,
                        load_admin=False, load_next_plan=False,
                        load_usage_logs=False, load_groups=False,
                    )
                except TypeError:
                    try:
                        existing = await get_user(db, username)
                    except Exception as e:
                        errors.append({"username": username, "error": f"get_user: {e}"})
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        continue
                except Exception as e:
                    errors.append({"username": username, "error": f"get_user: {e}"})
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    continue

            if existing is not None:
                skipped.append({
                    "username": username,
                    "reason": "already_exists",
                    "user_id": int(existing.id),
                    "matched_username": getattr(existing, "username", None),
                })
                try:
                    token = await make_sub_token(db, int(existing.id))
                    created.append({
                        "username": username,
                        "uuid": uuid_s,
                        "user_id": int(existing.id),
                        "subscription_url": f"/sub/{token}",
                        "reused": True,
                    })
                except Exception as e:
                    errors.append({"username": username, "error": f"exists but token failed: {e}"})
                continue

            want_disabled = (row.get("status") or "").strip() == "disabled"
            raw_status = (row.get("status") or "active").strip()
            create_status = "on_hold" if raw_status == "on_hold" and not want_disabled else "active"

            trojan_pw = uuid_s.replace("-", "")[:22]
            if len(trojan_pw) < 22:
                trojan_pw = (trojan_pw + "hiddify-migrate-pass00")[:22]

            try:
                proxy = ProxyTable(
                    vless=VlessSettings(id=uid),
                    vmess=VMessSettings(id=uid),
                    trojan=TrojanSettings(password=trojan_pw),
                )
            except Exception as e:
                errors.append({"username": username, "error": f"proxy: {e}"})
                continue

            body = {
                "username": username,
                "status": create_status,
                "data_limit": int(row.get("data_limit") or 0) or None,
                "data_limit_reset_strategy": row.get("data_limit_reset_strategy") or "no_reset",
                "note": (row.get("note") or "")[:500] or None,
                "proxy_settings": proxy,
                "group_ids": list(group_ids),
            }

            expire_raw = row.get("expire")
            if expire_raw and create_status != "on_hold":
                try:
                    body["expire"] = datetime.fromtimestamp(int(expire_raw), tz=timezone.utc)
                except Exception:
                    try:
                        body["expire"] = int(expire_raw)
                    except Exception:
                        pass

            if create_status == "on_hold":
                try:
                    hold_dur = int(row.get("on_hold_expire_duration") or 0)
                except Exception:
                    hold_dur = 0
                if hold_dur <= 0:
                    body["status"] = "active"
                else:
                    body["on_hold_expire_duration"] = hold_dur

            try:
                new_user = UserCreate(**body)
            except Exception:
                body["status"] = "active"
                body.pop("on_hold_expire_duration", None)
                body.pop("on_hold_timeout", None)
                try:
                    new_user = UserCreate(**body)
                except Exception as e2:
                    errors.append({"username": username, "error": f"validate: {e2}"})
                    continue

            try:
                user = await create_user(db, new_user, groups=list(groups), admin=owner)
                if want_disabled:
                    try:
                        user.status = UserStatus.disabled
                        await db.commit()
                    except Exception:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                token = await make_sub_token(db, int(user.id))
                used = int(row.get("used_traffic") or 0)
                if used > 0:
                    try:
                        user.used_traffic = used
                        await db.commit()
                    except Exception:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                created.append({
                    "username": username,
                    "uuid": uuid_s,
                    "user_id": int(user.id),
                    "subscription_url": f"/sub/{token}",
                    "reused": False,
                })
                uuid_index[str(uid).lower()] = user
            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
                errors.append({"username": username, "error": str(e)[:300]})

    write_result({
        "ok": True,
        "group": group_name,
        "group_id": group_id,
        "created": created,
        "errors": errors,
        "skipped": skipped,
        "created_count": len([c for c in created if not c.get("reused")]),
        "mapped_count": len(created),
    })


try:
    asyncio.run(main())
except SystemExit:
    pass
except Exception as e:
    write_result({
        "ok": False,
        "error": str(e)[:500],
        "traceback": traceback.format_exc(),
        "created": [],
        "errors": [{"username": "*", "error": str(e)[:300]}],
        "skipped": [],
    })
'''


def _python_bins() -> list[str]:
    return ["/code/.venv/bin/python", "python"]


async def run_hiddify_user_import(migrator, users: list[dict], work: Path) -> dict:
    """Write payload+script under PASARGUARD_DATA and execute inside panel image."""
    PASARGUARD_DATA.mkdir(parents=True, exist_ok=True)
    stamp = migrator.job.job_id
    payload_host = PASARGUARD_DATA / f"hiddify_import_{stamp}.json"
    result_host = PASARGUARD_DATA / f"hiddify_import_{stamp}_result.json"
    script_host = PASARGUARD_DATA / f"hiddify_import_{stamp}.py"

    payload_host.write_text(
        json.dumps({"users": users, "group": HIDDIFY_TEST_GROUP}, ensure_ascii=False),
        encoding="utf-8",
    )
    script_host.write_text(IMPORT_SCRIPT, encoding="utf-8")
    if result_host.exists():
        result_host.unlink()

    payload_c = f"/var/lib/pasarguard/{payload_host.name}"
    result_c = f"/var/lib/pasarguard/{result_host.name}"
    script_c = f"/var/lib/pasarguard/{script_host.name}"

    await migrator._run_cmd(
        ["docker", "compose", "up", "-d", resolve_pasarguard_service()],
        cwd=str(PASARGUARD_DIR),
        timeout=120,
    )

    timeout = max(180, min(2400, 45 + len(users) * 3))
    logs: list[str] = []
    ordered = [resolve_pasarguard_service(), *PASARGUARD_SERVICE_CANDIDATES]
    services: list[str] = []
    for s in ordered:
        if s not in services:
            services.append(s)

    # --- Strategy A: compose exec with /code on path ---
    for svc in services:
        for py in _python_bins():
            if result_host.is_file():
                break
            cmd = [
                "docker", "compose", "exec", "-T",
                "-w", "/code",
                "-e", "PYTHONPATH=/code",
                svc,
                py, script_c, payload_c, result_c,
            ]
            migrator.job.log(f"import via compose exec ({svc}, {py})…")
            ok, out = await migrator._run_cmd(cmd, cwd=str(PASARGUARD_DIR), timeout=timeout)
            logs.append(f"# compose exec {svc} {py} ok={ok}\n{(out or '')[-2000:]}")
            if result_host.is_file():
                break
        if result_host.is_file():
            break

    # --- Strategy B: docker run sharing the panel container network ---
    if not result_host.is_file():
        image = resolve_pasarguard_image()
        for svc in services[:2]:
            ok_cid, cid_out = await migrator._run_cmd(
                ["docker", "compose", "ps", "-q", svc],
                cwd=str(PASARGUARD_DIR),
                timeout=30,
            )
            cid = ""
            if ok_cid and cid_out:
                cid = cid_out.strip().splitlines()[0].strip()
            if not cid:
                continue
            # Pull env from running container so DB URL/hostnames match
            ok_env, env_out = await migrator._run_cmd(
                ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", cid],
                timeout=30,
            )
            env_file: Path | None = None
            try:
                if ok_env and env_out:
                    tmp = work / f"hiddify_import_{stamp}.container.env"
                    # docker run --env-file rejects blank lines / weird keys
                    lines = []
                    for line in env_out.splitlines():
                        if not line or "=" not in line or line.startswith("="):
                            continue
                        key = line.split("=", 1)[0]
                        if any(ch.isspace() for ch in key):
                            continue
                        lines.append(line)
                    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    env_file = tmp
                elif PASARGUARD_ENV.exists():
                    env_file = write_docker_env_file(PASARGUARD_ENV)

                cmd = [
                    "docker", "run", "--rm",
                    f"--network=container:{cid}",
                    "-e", "PYTHONPATH=/code",
                    "-v", f"{PASARGUARD_DATA}:/var/lib/pasarguard",
                    "-w", "/code",
                    "--entrypoint", "python",
                ]
                if env_file and env_file.exists():
                    cmd.extend(["--env-file", str(env_file)])
                cmd.extend([image, script_c, payload_c, result_c])
                migrator.job.log(f"import via docker run (network=container:{cid[:12]})…")
                ok, out = await migrator._run_cmd(cmd, timeout=timeout)
                logs.append(f"# docker run network=container ok={ok}\n{(out or '')[-2000:]}")
            finally:
                if env_file is not None:
                    try:
                        env_file.unlink(missing_ok=True)
                    except Exception:
                        pass
            if result_host.is_file():
                break

    # --- Strategy C: host-network run with sanitized .env (common PG installs) ---
    if not result_host.is_file() and PASARGUARD_ENV.exists():
        image = resolve_pasarguard_image()
        env_file = None
        try:
            env_file = write_docker_env_file(PASARGUARD_ENV)
            cmd = [
                "docker", "run", "--rm", "--network", "host",
                "--env-file", str(env_file),
                "-e", "PYTHONPATH=/code",
                "-v", f"{PASARGUARD_DATA}:/var/lib/pasarguard",
                "-w", "/code",
                "--entrypoint", "python",
                image, script_c, payload_c, result_c,
            ]
            migrator.job.log("import via docker run --network host…")
            ok, out = await migrator._run_cmd(cmd, timeout=timeout)
            logs.append(f"# docker run host ok={ok}\n{(out or '')[-2000:]}")
        finally:
            if env_file is not None:
                try:
                    env_file.unlink(missing_ok=True)
                except Exception:
                    pass

    combined = "\n".join(logs)
    try:
        (work / "hiddify_import_docker.log").write_text(combined, encoding="utf-8")
    except Exception:
        pass

    if not result_host.is_file():
        err = combined.strip() or "import script produced no result"
        if "Traceback" in err:
            err = err[err.rfind("Traceback"):]
        if "No module named" in err and "app" in err:
            err = (
                "PasarGuard panel Python could not import 'app'. "
                "Tried compose exec -w /code and docker run fallbacks. "
                f"Detail: {err[-400:]}"
            )
        return {
            "ok": False,
            "error": err[:900],
            "created": [],
            "errors": [{"username": "*", "error": err[:400]}],
            "skipped": [],
        }

    try:
        result = json.loads(result_host.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "ok": False,
            "error": f"bad result json: {e}",
            "created": [],
            "errors": [{"username": "*", "error": str(e)}],
            "skipped": [],
        }

    if not result.get("ok", True) and not (result.get("created") or []):
        fatal = result.get("error") or result.get("traceback") or ""
        if fatal:
            migrator.job.log(f"import fatal: {str(fatal)[:600]}")

    for p in (script_host, payload_host):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    return result
