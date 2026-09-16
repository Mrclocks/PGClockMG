"""Sanitize / recover ``hosts`` rows from PostgreSQL plain-SQL dumps.

Same-engine restore does not run ``convert_value``, so dump artifacts
(``{address}``, bad enums, invisible junk) can make ``COPY hosts`` fail or
leave zero rows after later alembic cleanup. This module rewrites hosts COPY
payloads and can reload them after restore when verify would see hosts:0/N.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.native_migration.copy_core import convert_value

_COPY_HOSTS_RE = re.compile(
    r'^COPY\s+(?:ONLY\s+)?(?:public\.)?"?hosts"?\s*\(([^)]*)\)\s+FROM\s+stdin\s*;\s*$',
    re.IGNORECASE,
)

# Columns we know how to coerce; unknown columns pass through (except \N).
_HOST_CONVERT_COLUMNS = frozenset({
    "address",
    "sni",
    "host",
    "verify_peer_cert_by_name",
    "remark",
    "security",
    "fingerprint",
    "alpn",
    "inbound_tag",
    "allowinsecure",
    "random_user_agent",
    "use_sni_as_host",
    "is_disabled",
    "priority",
    "mux_settings",
    "fragment_settings",
    "noise_settings",
    "http_headers",
    "transport_settings",
})


def _parse_columns(col_blob: str) -> list[str]:
    cols: list[str] = []
    for raw in col_blob.split(","):
        name = raw.strip().strip('"').strip("`")
        if name:
            cols.append(name)
    return cols


def _pg_dump_escape(value) -> str:
    """Encode a Python value as a PostgreSQL COPY text field."""
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except Exception:
            value = bytes(value).decode("latin-1", errors="replace")
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _decode_copy_field(raw: str) -> str | None:
    if raw == "\\N":
        return None
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            mapping = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "\\": "\\"}
            out.append(mapping.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def sanitize_hosts_copy_row(columns: list[str], line: str) -> str:
    """Sanitize one COPY data line for hosts; returns a COPY line (no trailing newline)."""
    fields = line.split("\t")
    if len(fields) < len(columns):
        fields.extend(["\\N"] * (len(columns) - len(fields)))
    elif len(fields) > len(columns):
        fields = fields[: len(columns)]

    out_fields: list[str] = []
    for col, raw in zip(columns, fields):
        decoded = _decode_copy_field(raw)
        if col in _HOST_CONVERT_COLUMNS:
            converted = convert_value("hosts", col, decoded)
            out_fields.append(_pg_dump_escape(converted))
        else:
            out_fields.append(_pg_dump_escape(decoded) if decoded is not None else "\\N")
    return "\t".join(out_fields)


def sanitize_hosts_copy_in_pg_dump(src: Path, dest: Path) -> dict[str, int]:
    """Rewrite ``COPY hosts`` payloads in a plain SQL dump.

    Returns ``{"rows": N}`` for sanitized data rows. Schema / other tables unchanged.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    in_hosts = False
    columns: list[str] = []
    with open(src, "r", encoding="utf-8", errors="replace") as inf, open(
        dest, "w", encoding="utf-8"
    ) as outf:
        for raw in inf:
            line = raw.rstrip("\n").rstrip("\r")
            if in_hosts:
                if line.startswith("\\.") or line.strip() == "\\.":
                    outf.write(raw if raw.endswith("\n") else raw + "\n")
                    in_hosts = False
                    columns = []
                    continue
                if line.strip().startswith("--"):
                    outf.write(raw if raw.endswith("\n") else raw + "\n")
                    continue
                sanitized = sanitize_hosts_copy_row(columns, line)
                outf.write(sanitized + "\n")
                rows += 1
                continue

            m = _COPY_HOSTS_RE.match(line)
            if m:
                columns = _parse_columns(m.group(1))
                in_hosts = bool(columns)
                outf.write(line + "\n")
                continue
            outf.write(raw if raw.endswith("\n") else raw + "\n")
    return {"rows": rows}


def extract_sanitized_hosts_copy_sql(src: Path) -> str | None:
    """Return a standalone ``COPY hosts (…) FROM stdin; … \\.`` block, sanitized.

    Used to reload hosts when post-restore verify would see ``hosts: 0/N``.
    """
    columns: list[str] = []
    rows: list[str] = []
    in_hosts = False
    header = ""
    with open(src, "r", encoding="utf-8", errors="replace") as inf:
        for raw in inf:
            line = raw.rstrip("\n").rstrip("\r")
            if in_hosts:
                if line.startswith("\\.") or line.strip() == "\\.":
                    break
                if line.strip().startswith("--"):
                    continue
                rows.append(sanitize_hosts_copy_row(columns, line))
                continue
            m = _COPY_HOSTS_RE.match(line)
            if m:
                columns = _parse_columns(m.group(1))
                if columns:
                    in_hosts = True
                    header = f'COPY public.hosts ({", ".join(columns)}) FROM stdin;'
    if not header or not rows:
        return None
    return header + "\n" + "\n".join(rows) + "\n\\.\n"


def count_hosts_rows_in_pg_dump(src: Path) -> int:
    """Count data rows inside the first ``COPY hosts`` block (unsanitized)."""
    in_hosts = False
    rows = 0
    with open(src, "r", encoding="utf-8", errors="replace") as inf:
        for raw in inf:
            line = raw.rstrip("\n").rstrip("\r")
            if in_hosts:
                if line.startswith("\\.") or line.strip() == "\\.":
                    break
                if line.strip().startswith("--") or not line.strip():
                    continue
                rows += 1
                continue
            if _COPY_HOSTS_RE.match(line):
                in_hosts = True
    return rows
