"""Optional Marzban→PasarGuard inbound TLS cert relocation.

When enabled, copies certificateFile/keyFile referenced by xray_config.json into
``/var/lib/pasarguard/certs/<domain>/``, sets readable permissions, and rewrites
paths in the JSON before panel boot seeds ``core_configs``.

PasarGuard restore must NOT use this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from app.config import MARZBAN_DATA, PASARGUARD_DATA

LogFn = Callable[[str], None]

_CERT_DIR_MODE = 0o755
_CERT_FILE_MODE = 0o644
_KEY_FILE_MODE = 0o600

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)
_ACME_DOMAIN_RE = re.compile(
    r"(?:^|/)([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?:_ecc|_rsa)?(?:/|$)"
)


def strip_json_comments(text: str) -> str:
    """Best-effort strip of // and /* */ comments from xray JSON."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    escape = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(n, i + 2)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_xray_config(text: str) -> dict[str, Any]:
    raw = text.lstrip("\ufeff")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(strip_json_comments(raw))
    if not isinstance(data, dict):
        raise ValueError("xray_config root must be an object")
    return data


def _safe_domain_slug(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower().strip(".")
    if not v or len(v) > 200:
        return None
    if _DOMAIN_RE.match(v):
        return v
    # Allow single-label hostnames used in lab setups
    if re.match(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", v):
        return v
    return None


def _domain_from_path(path: str) -> str | None:
    m = _ACME_DOMAIN_RE.search(path.replace("\\", "/"))
    if m:
        return _safe_domain_slug(m.group(1))
    parts = Path(path.replace("\\", "/")).parts
    for part in reversed(parts):
        slug = _safe_domain_slug(part)
        if slug:
            return slug
        # acme.sh style example.com_ecc
        if part.endswith(("_ecc", "_rsa")):
            slug = _safe_domain_slug(part.rsplit("_", 1)[0])
            if slug:
                return slug
    return None


def _pair_domain(cert_path: str, key_path: str, server_name: str | None) -> str:
    for candidate in (
        _safe_domain_slug(server_name),
        _domain_from_path(cert_path),
        _domain_from_path(key_path),
    ):
        if candidate:
            return candidate
    digest = hashlib.sha1(f"{cert_path}|{key_path}".encode()).hexdigest()[:10]
    return f"inbound-{digest}"


def _candidate_host_paths(raw: str) -> list[Path]:
    """Resolve Marzban/PasarGuard/acme paths that may exist on the host."""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return []
    paths: list[Path] = []
    p = Path(text)
    paths.append(p)
    if text.startswith("/var/lib/marzban/"):
        paths.append(PASARGUARD_DATA / text[len("/var/lib/marzban/") :])
        paths.append(MARZBAN_DATA / text[len("/var/lib/marzban/") :])
    elif text.startswith("/var/lib/pasarguard/"):
        paths.append(PASARGUARD_DATA / text[len("/var/lib/pasarguard/") :])
    elif text.startswith("/opt/marzban/"):
        paths.append(Path("/opt/pasarguard") / text[len("/opt/marzban/") :])
    elif not p.is_absolute():
        paths.append(MARZBAN_DATA / text)
        paths.append(PASARGUARD_DATA / text)
        paths.append(PASARGUARD_DATA / "certs" / text)
        paths.append(MARZBAN_DATA / "certs" / text)
    # Basename search under known certs trees
    name = p.name
    if name:
        for root in (PASARGUARD_DATA / "certs", MARZBAN_DATA / "certs"):
            if root.is_dir():
                hit = next((x for x in root.rglob(name) if x.is_file()), None)
                if hit:
                    paths.append(hit)
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for cand in paths:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def resolve_existing_file(raw: str) -> Path | None:
    for cand in _candidate_host_paths(raw):
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand.resolve()
        except OSError:
            continue
    return None


def _iter_certificate_dicts(node: Any):
    if isinstance(node, dict):
        certs = node.get("certificates")
        if isinstance(certs, list):
            for item in certs:
                if isinstance(item, dict):
                    yield item, node
        for value in node.values():
            yield from _iter_certificate_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_certificate_dicts(item)


def _server_name_near(parent: dict[str, Any]) -> str | None:
    for key in ("serverName", "server_name", "dest", "target"):
        val = parent.get(key)
        if isinstance(val, str) and val.strip():
            # reality dest may be host:port
            host = val.strip().split(":", 1)[0]
            slug = _safe_domain_slug(host)
            if slug:
                return slug
    return None


def apply_cert_permissions(path: Path, *, is_key: bool) -> None:
    try:
        if path.is_dir():
            os.chmod(path, _CERT_DIR_MODE)
        else:
            os.chmod(path, _KEY_FILE_MODE if is_key else _CERT_FILE_MODE)
    except OSError:
        pass


def _copy_file(src: Path, dst: Path, *, is_key: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    apply_cert_permissions(dst.parent, is_key=False)
    shutil.copy2(src, dst)
    apply_cert_permissions(dst, is_key=is_key)


def relocate_inbound_certs_in_xray_config(
    xray_path: Path,
    *,
    certs_root: Path | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Copy inbound TLS files into certs/<domain>/ and rewrite xray_config paths.

    Returns a summary dict. No-op summary when nothing to do.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    certs_root = Path(certs_root or (PASARGUARD_DATA / "certs"))
    summary: dict[str, Any] = {
        "rewritten": 0,
        "copied": 0,
        "skipped": 0,
        "missing": [],
        "domains": [],
    }
    if not xray_path.is_file():
        _log(f"Inbound cert relocate skipped — missing {xray_path}")
        return summary

    original = xray_path.read_text(encoding="utf-8", errors="ignore")
    try:
        data = load_xray_config(original)
    except Exception as e:
        raise RuntimeError(f"Could not parse xray_config.json for cert relocate: {e}") from e

    # Cache identical source pairs → destination paths
    pair_map: dict[tuple[str, str], tuple[str, str]] = {}

    for cert_obj, parent in _iter_certificate_dicts(data):
        cert_raw = cert_obj.get("certificateFile") or cert_obj.get("certificate_file")
        key_raw = cert_obj.get("keyFile") or cert_obj.get("key_file")
        if not isinstance(cert_raw, str) or not isinstance(key_raw, str):
            continue
        if not cert_raw.strip() or not key_raw.strip():
            continue

        src_key = (cert_raw.strip(), key_raw.strip())
        if src_key in pair_map:
            new_cert, new_key = pair_map[src_key]
            if cert_obj.get("certificateFile") != new_cert or cert_obj.get("keyFile") != new_key:
                cert_obj["certificateFile"] = new_cert
                cert_obj["keyFile"] = new_key
                if "certificate_file" in cert_obj:
                    cert_obj["certificate_file"] = new_cert
                if "key_file" in cert_obj:
                    cert_obj["key_file"] = new_key
                summary["rewritten"] += 1
            else:
                summary["skipped"] += 1
            continue

        cert_src = resolve_existing_file(cert_raw)
        key_src = resolve_existing_file(key_raw)
        if not cert_src or not key_src:
            summary["missing"].append({"certificateFile": cert_raw, "keyFile": key_raw})
            _log(
                "Inbound cert relocate: source file(s) not found for "
                f"cert={cert_raw!r} key={key_raw!r}"
            )
            continue

        domain = _pair_domain(str(cert_src), str(key_src), _server_name_near(parent))
        dest_dir = certs_root / domain
        # Prefer stable names; keep original suffix when not pem
        cert_name = "fullchain.pem" if cert_src.suffix.lower() in {".pem", ".crt", ".cer", ""} else cert_src.name
        key_name = "privkey.pem" if key_src.suffix.lower() in {".pem", ".key", ""} else key_src.name
        # Avoid clobbering different content under same domain names
        dest_cert = dest_dir / cert_name
        dest_key = dest_dir / key_name
        if dest_cert.exists() and dest_cert.resolve() != cert_src.resolve():
            try:
                if dest_cert.read_bytes() != cert_src.read_bytes():
                    digest = hashlib.sha1(str(cert_src).encode()).hexdigest()[:8]
                    dest_cert = dest_dir / f"{digest}-{cert_name}"
                    dest_key = dest_dir / f"{digest}-{key_name}"
            except OSError:
                digest = hashlib.sha1(str(cert_src).encode()).hexdigest()[:8]
                dest_cert = dest_dir / f"{digest}-{cert_name}"
                dest_key = dest_dir / f"{digest}-{key_name}"

        already_in_place = (
            cert_src.resolve() == dest_cert.resolve()
            and key_src.resolve() == dest_key.resolve()
        )
        if not already_in_place:
            _copy_file(cert_src, dest_cert, is_key=False)
            _copy_file(key_src, dest_key, is_key=True)
            summary["copied"] += 1
            _log(f"Inbound certs → {dest_dir}/ ({domain})")
        else:
            apply_cert_permissions(dest_dir, is_key=False)
            apply_cert_permissions(dest_cert, is_key=False)
            apply_cert_permissions(dest_key, is_key=True)
            summary["skipped"] += 1

        new_cert = f"/var/lib/pasarguard/certs/{domain}/{dest_cert.name}"
        new_key = f"/var/lib/pasarguard/certs/{domain}/{dest_key.name}"
        pair_map[src_key] = (new_cert, new_key)
        cert_obj["certificateFile"] = new_cert
        cert_obj["keyFile"] = new_key
        if "certificate_file" in cert_obj:
            cert_obj["certificate_file"] = new_cert
        if "key_file" in cert_obj:
            cert_obj["key_file"] = new_key
        summary["rewritten"] += 1
        if domain not in summary["domains"]:
            summary["domains"].append(domain)

    if summary["rewritten"] or summary["copied"]:
        xray_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _log(
            "Inbound cert relocate done: "
            f"copied={summary['copied']} rewritten={summary['rewritten']} "
            f"domains={len(summary['domains'])} missing={len(summary['missing'])}"
        )
    elif summary["missing"]:
        _log(
            f"Inbound cert relocate: no files copied; "
            f"{len(summary['missing'])} path pair(s) missing on disk"
        )
    else:
        _log("Inbound cert relocate: nothing to change")
    return summary
