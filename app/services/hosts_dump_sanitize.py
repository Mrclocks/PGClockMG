"""Extract / reload ``hosts`` from PostgreSQL dumps — permanent same-engine path.

Same-engine restore used to rely on raw ``COPY hosts`` inside the dump. That
fails silently under schema drift, missing column lists, address junk, or
post-import alembic wipes — leaving verify at ``hosts: 0/N`` while users look
fine.

Strategy:
1. Strip hosts *data* from the dump before import (DDL kept).
2. After alembic, parse hosts rows from the original dump (COPY w/ or w/o
   columns, or INSERT), run ``convert_value``, and INSERT into the live table.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.services.native_migration.copy_core import (
    TARGET_INSERT_DEFAULTS,
    convert_value,
)

# Flexible COPY head — matches sql_dump_counts (optional column list, quotes).
_COPY_HOSTS_RE = re.compile(
    r"""(?ix)
    ^COPY\s+(?:ONLY\s+)?
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<q1>["`]?)hosts(?P=q1)
    \s*(?:\((?P<cols>[^;]*)\))?
    \s+FROM\s+stdin\s*;
    \s*$
    """
)

_INSERT_HOSTS_RE = re.compile(
    r"""(?ix)
    ^INSERT\s+INTO\s+
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<q1>["`\[]?)hosts(?P=q1)
    \s*(?:\((?P<cols>[^)]*)\))?
    \s*VALUES\s*(?P<values>.*)$
    """
)

_CREATE_HOSTS_RE = re.compile(
    r"""(?ix)
    CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<q1>["`]?)hosts(?P=q1)
    \s*\(
    """
)

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


def _parse_columns(col_blob: str | None) -> list[str]:
    if not col_blob:
        return []
    cols: list[str] = []
    for raw in col_blob.split(","):
        name = raw.strip().strip('"').strip("`").strip("[]")
        if name:
            cols.append(name)
    return cols


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


def _sql_literal(value: Any) -> str:
    """Encode a Python value as a PostgreSQL SQL literal for INSERT."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except Exception:
            value = bytes(value).decode("latin-1", errors="replace")
    s = str(value)
    return "'" + s.replace("'", "''") + "'"


def _parse_create_table_columns(sql_blob: str) -> list[str]:
    """Best-effort column names from a CREATE TABLE hosts (…) DDL blob."""
    cols: list[str] = []
    depth = 0
    token: list[str] = []
    i = 0
    # Find opening paren of CREATE TABLE hosts
    m = _CREATE_HOSTS_RE.search(sql_blob)
    if not m:
        return []
    start = m.end() - 1  # at '('
    i = start
    while i < len(sql_blob):
        ch = sql_blob[i]
        if ch == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        if depth == 1 and ch == ",":
            piece = "".join(token).strip()
            token = []
            if piece and not piece.upper().startswith(
                ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")
            ):
                name = piece.split()[0].strip('"').strip("`")
                if name and name.upper() not in ("CONSTRAINT",):
                    cols.append(name)
            i += 1
            continue
        if depth >= 1:
            token.append(ch)
        i += 1
    piece = "".join(token).strip()
    if piece and not piece.upper().startswith(
        ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN")
    ):
        name = piece.split()[0].strip('"').strip("`")
        if name:
            cols.append(name)
    return cols


def _read_create_hosts_columns(src: Path) -> list[str]:
    """Stream dump until CREATE TABLE hosts is complete; return column names."""
    collecting = False
    buf: list[str] = []
    depth = 0
    with open(src, "r", encoding="utf-8", errors="replace") as inf:
        for raw in inf:
            if not collecting:
                if _CREATE_HOSTS_RE.search(raw):
                    collecting = True
                    buf.append(raw)
                    depth += raw.count("(") - raw.count(")")
                    if depth <= 0 and "(" in raw:
                        break
                continue
            buf.append(raw)
            depth += raw.count("(") - raw.count(")")
            if depth <= 0:
                break
    return _parse_create_table_columns("".join(buf))


def _iter_sql_value_tuples(values_blob: str) -> list[str]:
    """Extract top-level ``(…)`` tuples from an INSERT VALUES blob."""
    tuples: list[str] = []
    depth = 0
    cur: list[str] = []
    in_quote = False
    i = 0
    while i < len(values_blob):
        ch = values_blob[i]
        if in_quote:
            cur.append(ch)
            if ch == "'" and i + 1 < len(values_blob) and values_blob[i + 1] == "'":
                cur.append(values_blob[i + 1])
                i += 2
                continue
            if ch == "'":
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            if depth >= 1:
                cur.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            if depth == 1:
                cur = []
            else:
                cur.append(ch)
            i += 1
            continue
        if ch == ")":
            if depth == 1:
                tuples.append("".join(cur))
                depth = 0
                cur = []
            elif depth > 1:
                cur.append(ch)
                depth -= 1
            i += 1
            continue
        if depth >= 1:
            cur.append(ch)
        i += 1
    return tuples


def _row_dict(columns: list[str], fields: list[str | None]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, col in enumerate(columns):
        raw = fields[i] if i < len(fields) else None
        if col in _HOST_CONVERT_COLUMNS:
            out[col] = convert_value("hosts", col, raw)
        else:
            out[col] = raw
    defaults = TARGET_INSERT_DEFAULTS.get("hosts", {})
    for col, default in defaults.items():
        if col not in out or out[col] is None:
            if col in _HOST_CONVERT_COLUMNS:
                out[col] = convert_value("hosts", col, default)
            else:
                out[col] = default
    if not str(out.get("remark") or "").strip():
        out["remark"] = "host"
    if out.get("address") is None:
        out["address"] = ""
    return out


def _split_sql_tuple(tup: str) -> list[str]:
    """Split ``a, 'b,c', NULL`` into fields honoring quotes."""
    fields: list[str] = []
    cur: list[str] = []
    in_quote = False
    i = 0
    while i < len(tup):
        ch = tup[i]
        if in_quote:
            cur.append(ch)
            if ch == "'" and i + 1 < len(tup) and tup[i + 1] == "'":
                cur.append(tup[i + 1])
                i += 2
                continue
            if ch == "'":
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            cur.append(ch)
            i += 1
            continue
        if ch == ",":
            fields.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if cur or fields:
        fields.append("".join(cur).strip())
    return fields


def _finish_insert_blob(blob: str, create_cols: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    """Parse one INSERT INTO hosts … VALUES …; blob → (columns, rows)."""
    hm = re.match(
        r"(?is)^\s*INSERT\s+INTO\s+(?:[A-Za-z_][\w]*\.)?[\"`\[]?hosts[\"`\]]?\s*"
        r"(?:\(([^)]*)\))?\s*VALUES\s*(.*)$",
        blob.strip().rstrip(";").strip(),
    )
    if not hm:
        return [], []
    cols = _parse_columns(hm.group(1)) or list(create_cols)
    if not cols:
        return [], []
    rows: list[dict[str, Any]] = []
    for tup in _iter_sql_value_tuples(hm.group(2)):
        fields = _split_sql_tuple(tup)
        decoded: list[str | None] = []
        for f in fields:
            f = f.strip()
            if f.upper() == "NULL":
                decoded.append(None)
            elif len(f) >= 2 and f[0] == "'" and f[-1] == "'":
                decoded.append(f[1:-1].replace("''", "'"))
            else:
                decoded.append(f)
        rows.append(_row_dict(cols, decoded))
    return cols, rows


def extract_hosts_rows_from_pg_dump(src: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Return ``(source_columns, rows_as_dicts)`` from the first hosts data section."""
    create_cols = _read_create_hosts_columns(src)
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    mode: str | None = None
    insert_buf: list[str] = []

    with open(src, "r", encoding="utf-8", errors="replace") as inf:
        for raw in inf:
            line = raw.rstrip("\n").rstrip("\r")

            if mode == "copy":
                if line.startswith("\\.") or line.strip() == "\\.":
                    mode = None
                    continue
                if line.strip().startswith("--") or not line.strip():
                    continue
                fields = [_decode_copy_field(f) for f in line.split("\t")]
                rows.append(_row_dict(columns, fields))
                continue

            if mode == "insert":
                insert_buf.append(raw)
                if ";" in line:
                    cols, new_rows = _finish_insert_blob("".join(insert_buf), create_cols)
                    if cols:
                        columns = cols
                    rows.extend(new_rows)
                    mode = None
                    insert_buf = []
                continue

            cm = _COPY_HOSTS_RE.match(line.lstrip("\ufeff"))
            if cm:
                cols = _parse_columns(cm.group("cols")) or create_cols
                if not cols:
                    mode = "copy_skip"
                    columns = []
                    continue
                columns = cols
                mode = "copy"
                continue

            if mode == "copy_skip":
                if line.startswith("\\.") or line.strip() == "\\.":
                    mode = None
                continue

            im = _INSERT_HOSTS_RE.match(line.lstrip("\ufeff").lstrip())
            if im:
                mode = "insert"
                insert_buf = [raw]
                if ";" in line:
                    cols, new_rows = _finish_insert_blob("".join(insert_buf), create_cols)
                    if cols:
                        columns = cols
                    rows.extend(new_rows)
                    mode = None
                    insert_buf = []
                continue

    if mode == "insert" and insert_buf:
        cols, new_rows = _finish_insert_blob("".join(insert_buf), create_cols)
        if cols:
            columns = cols
        rows.extend(new_rows)

    return columns, rows


def count_hosts_rows_in_pg_dump(src: Path) -> int:
    """Count hosts data rows (COPY or INSERT) for recover gating."""
    _cols, rows = extract_hosts_rows_from_pg_dump(src)
    return len(rows)


def strip_hosts_copy_data_from_pg_dump(src: Path, dest: Path) -> int:
    """Keep hosts DDL but drop COPY/INSERT *data* so import cannot leave hosts half-broken.

    Returns number of data rows stripped.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    skipped = 0
    mode: str | None = None
    with open(src, "r", encoding="utf-8", errors="replace") as inf, open(
        dest, "w", encoding="utf-8"
    ) as outf:
        for raw in inf:
            line = raw.rstrip("\n").rstrip("\r")

            if mode == "copy":
                if line.startswith("\\.") or line.strip() == "\\.":
                    outf.write(raw if raw.endswith("\n") else raw + "\n")
                    mode = None
                else:
                    if line.strip() and not line.strip().startswith("--"):
                        skipped += 1
                continue

            if mode == "insert":
                if ";" in line:
                    mode = None
                    skipped += 1
                else:
                    skipped += 1
                continue

            if _COPY_HOSTS_RE.match(line.lstrip("\ufeff")):
                outf.write(raw if raw.endswith("\n") else raw + "\n")
                mode = "copy"
                continue

            if _INSERT_HOSTS_RE.match(line.lstrip("\ufeff").lstrip()):
                # Drop the whole INSERT (data only — no DDL here)
                if ";" in line:
                    skipped += 1
                else:
                    mode = "insert"
                    skipped += 1
                continue

            outf.write(raw if raw.endswith("\n") else raw + "\n")
    return skipped


def build_hosts_insert_sql(
    rows: list[dict[str, Any]],
    target_columns: list[str],
    *,
    inbound_tags: list[str] | None = None,
) -> str | None:
    """Build ``DELETE`` + ``INSERT`` SQL for hosts using live target columns."""
    if not rows or not target_columns:
        return None
    defaults = TARGET_INSERT_DEFAULTS.get("hosts", {})
    present: set[str] = set()
    for row in rows:
        present.update(row.keys())
    use_cols = [
        c for c in target_columns
        if c in present or c in defaults
    ]
    if not use_cols:
        return None

    tags = [t for t in (inbound_tags or []) if t]
    default_tag = tags[0] if tags else None
    tag_fold = {t.lower().strip(): t for t in tags}

    parts: list[str] = [
        "BEGIN;",
        "DELETE FROM public.hosts;",
    ]
    values_sql: list[str] = []
    for row in rows:
        row = dict(row)
        if "inbound_tag" in use_cols:
            tag = row.get("inbound_tag")
            if tag is not None:
                tag_s = str(tag).strip()
                if tags:
                    if tag_s not in tags:
                        mapped = tag_fold.get(tag_s.lower())
                        row["inbound_tag"] = mapped or default_tag
                elif not tag_s and default_tag:
                    row["inbound_tag"] = default_tag
            elif default_tag:
                row["inbound_tag"] = default_tag

        vals = []
        for col in use_cols:
            if col in row and row[col] is not None:
                vals.append(_sql_literal(row[col]))
            elif col in defaults:
                vals.append(_sql_literal(defaults[col]))
            elif col in row:
                vals.append(_sql_literal(row[col]))
            else:
                vals.append("NULL")
        values_sql.append("(" + ", ".join(vals) + ")")

    col_list = ", ".join(f'"{c}"' for c in use_cols)
    chunk = 50
    for i in range(0, len(values_sql), chunk):
        batch = values_sql[i : i + chunk]
        parts.append(
            f'INSERT INTO public.hosts ({col_list}) VALUES\n'
            + ",\n".join(batch)
            + ";"
        )
    parts.append("COMMIT;")
    return "\n".join(parts) + "\n"


# --- Back-compat helpers used by older recover / tests ----------------------

def sanitize_hosts_copy_row(columns: list[str], line: str) -> str:
    """Sanitize one COPY data line; returns COPY text fields (tab-separated)."""
    fields = line.split("\t")
    if len(fields) < len(columns):
        fields.extend(["\\N"] * (len(columns) - len(fields)))
    elif len(fields) > len(columns):
        fields = fields[: len(columns)]
    decoded = [_decode_copy_field(f) for f in fields]
    row = _row_dict(columns, decoded)
    out: list[str] = []
    for col in columns:
        val = row.get(col)
        if val is None:
            out.append("\\N")
        elif isinstance(val, bool):
            out.append("t" if val else "f")
        else:
            s = str(val)
            out.append(
                s.replace("\\", "\\\\")
                .replace("\t", "\\t")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
            )
    return "\t".join(out)


def sanitize_hosts_copy_in_pg_dump(src: Path, dest: Path) -> dict[str, int]:
    """Legacy: rewrite COPY hosts payloads. Prefer strip + ensure_hosts instead."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    in_hosts = False
    columns: list[str] = []
    create_cols = _read_create_hosts_columns(src)
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
                if columns:
                    outf.write(sanitize_hosts_copy_row(columns, line) + "\n")
                    rows += 1
                else:
                    outf.write(raw if raw.endswith("\n") else raw + "\n")
                continue
            m = _COPY_HOSTS_RE.match(line.lstrip("\ufeff"))
            if m:
                columns = _parse_columns(m.group("cols")) or create_cols
                in_hosts = True
                if columns and not m.group("cols"):
                    outf.write(
                        f'COPY public.hosts ({", ".join(columns)}) FROM stdin;\n'
                    )
                else:
                    outf.write(line + "\n")
                continue
            outf.write(raw if raw.endswith("\n") else raw + "\n")
    return {"rows": rows}


def extract_sanitized_hosts_copy_sql(src: Path) -> str | None:
    """Legacy COPY block builder; prefer ``build_hosts_insert_sql``."""
    columns, rows = extract_hosts_rows_from_pg_dump(src)
    if not columns or not rows:
        return None
    lines = [sanitize_hosts_copy_row(columns, "\t".join(
        ("\\N" if r.get(c) is None else str(r.get(c)) for c in columns)
    )) for r in rows]
    # Rebuild properly via sanitize path from dicts
    out_lines: list[str] = []
    for row in rows:
        fields: list[str] = []
        for c in columns:
            val = row.get(c)
            if val is None:
                fields.append("\\N")
            elif isinstance(val, bool):
                fields.append("t" if val else "f")
            else:
                s = str(val)
                fields.append(
                    s.replace("\\", "\\\\")
                    .replace("\t", "\\t")
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                )
        out_lines.append("\t".join(fields))
    header = f'COPY public.hosts ({", ".join(columns)}) FROM stdin;'
    return header + "\n" + "\n".join(out_lines) + "\n\\.\n"


def find_hosts_dump(paths: list[Path]) -> Path | None:
    """Return the first dump path that contains hosts data rows."""
    for path in paths:
        if not path or not Path(path).exists():
            continue
        try:
            n = count_hosts_rows_in_pg_dump(Path(path))
        except OSError:
            continue
        if n > 0:
            return Path(path)
    return None
