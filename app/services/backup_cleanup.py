"""Shrink a PasarGuard backup archive before it is restored.

Contract: this module never mutates its input. It reads one backup zip and
writes a new one, so the original upload always stays available as a fallback.
Nothing in the restore path is aware of it — a cleaned archive is handed back as
an ordinary upload and re-analyzed from scratch.

Scope is deliberately narrow. Only whole-table data removal is supported, and
only for traffic/usage history tables that the panel refills on its own. Those
tables routinely hold well over 90% of a backup's bytes and are what makes the
``use bigint for id`` alembic step run for hours on large panels. Row-level
predicates on a SQL dump would mean parsing SQL, which is where silent data
corruption comes from, so it is out of scope here.

The measure pass and the apply pass run the same filter over the same input; the
only difference is whether output is written. A preview therefore reports what
the apply will actually do rather than an estimate.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# Tables whose contents are traffic/usage history. Removing them costs the panel
# nothing it cannot rebuild: usage is re-accumulated from live node traffic.
# Every name here is asserted absent from copy_core's critical-table sets, so a
# cleaned archive still passes the post-restore verification unchanged.


@dataclass(frozen=True)
class CleanupRule:
    id: str
    tables: tuple[str, ...]
    default_enabled: bool
    label: dict[str, str]
    description: dict[str, str]


CLEANUP_RULES: tuple[CleanupRule, ...] = (
    CleanupRule(
        id="node_traffic_history",
        tables=("node_user_usages", "node_usages", "node_usage_reset_logs"),
        default_enabled=True,
        label={
            "en": "Node traffic history",
            "fa": "تاریخچه ترافیک نودها",
            "ru": "История трафика узлов",
        },
        description={
            "en": "Per-user and per-node traffic counters. The panel rebuilds these from live traffic; user quotas and totals are not affected.",
            "fa": "شمارنده‌های ترافیک هر کاربر و هر نود. پنل دوباره از ترافیک زنده می‌سازدشان؛ سهمیه و مصرف کل کاربران دست نمی‌خورد.",
            "ru": "Счётчики трафика по пользователям и узлам. Панель наполнит их заново; квоты пользователей не затрагиваются.",
        },
    ),
    CleanupRule(
        id="usage_logs",
        tables=("user_usage_logs", "admin_usage_logs", "node_stats"),
        default_enabled=True,
        label={
            "en": "Usage log tables",
            "fa": "جدول‌های لاگ مصرف",
            "ru": "Таблицы журналов использования",
        },
        description={
            "en": "Historical usage log rows, including legacy Marzban tables the panel no longer reads.",
            "fa": "ردیف‌های لاگ مصرف قدیمی، شامل جدول‌های به‌جامانده از Marzban که پنل دیگر نمی‌خواندشان.",
            "ru": "Старые записи журналов использования, включая устаревшие таблицы Marzban.",
        },
    ),
    CleanupRule(
        id="subscription_update_log",
        tables=("user_subscription_updates",),
        default_enabled=True,
        label={
            "en": "Subscription fetch log",
            "fa": "لاگ دریافت اشتراک",
            "ru": "Журнал обновлений подписки",
        },
        description={
            "en": "One row per subscription link fetch. Purely a log — subscription links keep working.",
            "fa": "برای هر بار باز شدن لینک اشتراک یک ردیف. صرفاً لاگ است — لینک‌های اشتراک سالم می‌مانند.",
            "ru": "Строка на каждое обращение к ссылке подписки. Только журнал — ссылки продолжают работать.",
        },
    ),
    CleanupRule(
        id="notification_reminders",
        tables=("notification_reminders", "admin_notification_reminders"),
        default_enabled=False,
        label={
            "en": "Pending notification reminders",
            "fa": "یادآورهای اعلان در انتظار",
            "ru": "Ожидающие напоминания",
        },
        description={
            "en": "Tracks which expiry/quota warnings were already sent. Clearing it may resend a warning users already received.",
            "fa": "نگه می‌دارد کدام هشدار انقضا/سهمیه قبلاً فرستاده شده. با پاک کردنش ممکن است هشداری دوباره برای کاربر ارسال شود.",
            "ru": "Хранит, какие предупреждения уже отправлены. После очистки они могут прийти повторно.",
        },
    ),
)

RULES_BY_ID: dict[str, CleanupRule] = {r.id: r for r in CLEANUP_RULES}

CLEANABLE_TABLES: frozenset[str] = frozenset(t for r in CLEANUP_RULES for t in r.tables)


def default_rule_ids() -> list[str]:
    return [r.id for r in CLEANUP_RULES if r.default_enabled]


def resolve_tables(rule_ids: list[str] | tuple[str, ...] | None) -> set[str]:
    """Map rule ids to table names, ignoring unknown ids."""
    out: set[str] = set()
    for rid in rule_ids or ():
        rule = RULES_BY_ID.get(rid)
        if rule:
            out.update(rule.tables)
    return out


# --------------------------------------------------------------------------
# SQL dump filtering
# --------------------------------------------------------------------------

# COPY public."node_usages" (id, ...) FROM stdin;
_COPY_RE = re.compile(
    r"""^\s*COPY\s+
        (?:(?:"[^"]+"|`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*)\s*\.\s*)?   # optional schema
        (?:"(?P<q>[^"]+)"|`(?P<b>[^`]+)`|(?P<p>[A-Za-z_][A-Za-z0-9_$]*))
        \s*(?:\([^)]*\))?\s+FROM\s+stdin\s*;\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# INSERT INTO `node_usages` (...) VALUES (...),(...);
# \b belongs only after the bare form: after a closing quote or backtick both
# sides are non-word characters, so a trailing \b there would never match.
_INSERT_RE = re.compile(
    r"""^\s*(?:INSERT|REPLACE)\s+(?:LOW_PRIORITY\s+|DELAYED\s+|HIGH_PRIORITY\s+|IGNORE\s+)*INTO\s+
        (?:(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)\s*\.\s*)?  # optional schema
        (?:"(?P<q>[^"]+)"|`(?P<b>[^`]+)`|\[(?P<s>[^\]]+)\]|(?P<p>[A-Za-z_][A-Za-z0-9_$]*)\b)""",
    re.IGNORECASE | re.VERBOSE,
)

# pg COPY data uses \. alone on a line as terminator. A literal backslash inside
# data is written as \\, so a data row can never be mistaken for the terminator.
_COPY_END = "\\."


def _byte_len(text: str) -> int:
    """Byte size of text that may carry surrogateescape-decoded raw bytes."""
    return len(text.encode("utf-8", "surrogateescape"))


def _matched_table(m: re.Match) -> str:
    groups = m.groupdict()
    for key in ("q", "b", "s", "p"):
        if groups.get(key):
            return groups[key]
    return ""


def _statement_is_complete(text: str) -> bool:
    """True when text ends a SQL statement outside any quoted literal.

    Tracks single/double quote and backtick state, backslash escapes and doubled
    quotes, so a ';' or newline inside a string value never ends the statement.
    """
    quote: str | None = None
    i = 0
    n = len(text)
    last_semicolon_at_end = False
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                # '' inside a '-quoted literal is an escaped quote, not the end
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            i += 1
            continue
        if c == ";":
            last_semicolon_at_end = True
            i += 1
            # only trailing whitespace may follow for the statement to be over
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i < n:
                last_semicolon_at_end = False
            continue
        i += 1
    return quote is None and last_semicolon_at_end


@dataclass
class FilterStats:
    """Per-table tally of what the filter removed."""

    rows: dict[str, int] = field(default_factory=dict)
    bytes: dict[str, int] = field(default_factory=dict)

    def add(self, table: str, rows: int, nbytes: int) -> None:
        if rows:
            self.rows[table] = self.rows.get(table, 0) + rows
        if nbytes:
            self.bytes[table] = self.bytes.get(table, 0) + nbytes

    def merge(self, other: "FilterStats") -> None:
        for t, v in other.rows.items():
            self.rows[t] = self.rows.get(t, 0) + v
        for t, v in other.bytes.items():
            self.bytes[t] = self.bytes.get(t, 0) + v

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    @property
    def total_bytes(self) -> int:
        return sum(self.bytes.values())


def filter_sql_stream(src, dst, tables: set[str]) -> FilterStats:
    """Copy SQL from src to dst, dropping data rows for `tables`.

    Reads and writes line by line so a multi-gigabyte dump never lands in
    memory. Schema (CREATE TABLE, indexes, constraints, sequence setval) is
    preserved untouched; only row payloads are removed.

    Anything not positively recognised as a data statement for a target table is
    emitted verbatim, so a parse the filter does not understand keeps data
    rather than losing it. Pass dst=None to measure without writing.
    """
    stats = FilterStats()
    write = dst.write if dst is not None else (lambda _s: None)
    measure = _byte_len

    in_copy_table: str | None = None
    copy_dropping = False

    pending: list[str] = []
    pending_table: str | None = None

    line_iter = iter(src)
    for line in line_iter:
        if in_copy_table is not None:
            if line.rstrip("\r\n") == _COPY_END:
                write(line)
                in_copy_table = None
                copy_dropping = False
                continue
            if copy_dropping:
                if line.strip():
                    stats.add(in_copy_table, 1, measure(line))
                continue
            write(line)
            continue

        if pending_table is not None:
            pending.append(line)
            if _statement_is_complete("".join(pending)):
                stmt = "".join(pending)
                stats.add(pending_table, _count_value_tuples(stmt), measure(stmt))
                pending = []
                pending_table = None
            continue

        m = _COPY_RE.match(line)
        if m:
            table = _matched_table(m)
            in_copy_table = table
            copy_dropping = table in tables
            # Keep the COPY header and its terminator even when dropping rows:
            # the statement stays structurally intact, just with no payload.
            write(line)
            continue

        m = _INSERT_RE.match(line)
        if m and _matched_table(m) in tables:
            table = _matched_table(m)
            if _statement_is_complete(line):
                stats.add(table, _count_value_tuples(line), measure(line))
            else:
                pending = [line]
                pending_table = table
            continue

        write(line)

    # An unterminated statement at EOF means the dump is truncated. Emit what was
    # buffered rather than silently swallowing it.
    if pending:
        stmt = "".join(pending)
        write(stmt)
        if pending_table:
            stats.rows.pop(pending_table, None)
            stats.bytes.pop(pending_table, None)

    return stats


_VALUES_SPLIT = re.compile(r"\bVALUES\b", re.IGNORECASE)


def _count_value_tuples(statement: str) -> int:
    """Count row tuples in an INSERT ... VALUES statement, ignoring literals."""
    parts = _VALUES_SPLIT.split(statement, maxsplit=1)
    if len(parts) < 2:
        return 1
    body = parts[1]
    quote: str | None = None
    depth = 0
    rows = 0
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                if i + 1 < n and body[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
        elif c == "(":
            if depth == 0:
                rows += 1
            depth += 1
        elif c == ")":
            depth = max(depth - 1, 0)
        i += 1
    return rows or 1


def _sql_files(root: Path) -> list[Path]:
    """Dump files in a backup, both layouts."""
    out: list[Path] = []
    single = root / "db_backup.sql"
    if single.is_file():
        out.append(single)
    pg_dir = root / "pg_dump"
    if pg_dir.is_dir():
        out.extend(sorted(p for p in pg_dir.glob("*.sql") if p.is_file()))
    return out


def _sqlite_files(root: Path) -> list[Path]:
    found = [p for p in [root / "db.sqlite3"] if p.is_file()]
    if not found:
        found = sorted(p for p in root.rglob("db.sqlite3") if p.is_file())
    return found


# --------------------------------------------------------------------------
# SQLite handling
# --------------------------------------------------------------------------


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return bool(row)


def measure_sqlite(path: Path, tables: set[str]) -> FilterStats:
    stats = FilterStats()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for table in sorted(tables):
            if not _sqlite_table_exists(conn, table):
                continue
            rows = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if not rows:
                continue
            nbytes = 0
            try:
                row = conn.execute(
                    "SELECT SUM(pgsize) FROM dbstat WHERE name=?", (table,)
                ).fetchone()
                nbytes = int(row[0] or 0)
            except sqlite3.Error:
                nbytes = 0
            stats.add(table, rows, nbytes)
    finally:
        conn.close()
    return stats


def clean_sqlite(path: Path, tables: set[str]) -> FilterStats:
    """Delete rows in-place from an already-copied SQLite file, then VACUUM."""
    stats = measure_sqlite(path, tables)
    if not stats.rows:
        return stats
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in sorted(stats.rows):
            conn.execute(f'DELETE FROM "{table}"')
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return stats


# --------------------------------------------------------------------------
# Archive level
# --------------------------------------------------------------------------


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"Unsafe zip entry: {info.filename}")
        target = dest / name
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _find_backup_root(extracted: Path) -> Path:
    """Same root resolution the restore analyzer uses."""
    for p in [extracted / ".env", *extracted.rglob(".env")]:
        if p.is_file() and p.name == ".env":
            return p.parent
    for cand in [extracted, *extracted.iterdir()]:
        if cand.is_dir() and (
            (cand / "db_backup.sql").exists()
            or (cand / "pg_dump" / "manifest.tsv").exists()
            or (cand / "db.sqlite3").exists()
        ):
            return cand
    return extracted


def measure_tree(root: Path, tables: set[str]) -> FilterStats:
    """What a cleanup of this extracted backup would remove."""
    stats = FilterStats()
    if not tables:
        return stats
    for sql in _sql_files(root):
        with open(sql, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            stats.merge(filter_sql_stream(fh, None, tables))
    for db in _sqlite_files(root):
        stats.merge(measure_sqlite(db, tables))
    return stats


def clean_tree(root: Path, tables: set[str]) -> FilterStats:
    """Rewrite dumps in an extracted backup in place. Caller owns the copy."""
    stats = FilterStats()
    if not tables:
        return stats
    for sql in _sql_files(root):
        tmp = sql.with_suffix(sql.suffix + ".cleaning")
        with open(sql, "r", encoding="utf-8", errors="surrogateescape", newline="") as src, open(
            tmp, "w", encoding="utf-8", errors="surrogateescape", newline=""
        ) as dst:
            stats.merge(filter_sql_stream(src, dst, tables))
        os.replace(tmp, sql)
    for db in _sqlite_files(root):
        stats.merge(clean_sqlite(db, tables))
    return stats


def _zip_tree(src_dir: Path, dest_zip: Path, arc_root: Path) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(arc_root).as_posix())


def _rule_breakdown(stats: FilterStats, rule_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for rid in rule_ids:
        rule = RULES_BY_ID.get(rid)
        if not rule:
            continue
        rows = sum(stats.rows.get(t, 0) for t in rule.tables)
        nbytes = sum(stats.bytes.get(t, 0) for t in rule.tables)
        out.append({
            "id": rule.id,
            "label": rule.label,
            "description": rule.description,
            "default_enabled": rule.default_enabled,
            "tables": list(rule.tables),
            "rows": rows,
            "bytes": nbytes,
            "table_rows": {t: stats.rows.get(t, 0) for t in rule.tables if stats.rows.get(t)},
        })
    return out


def analyze_cleanup(zip_path: str | Path) -> dict:
    """Report, per rule, exactly what a cleanup would remove from this archive.

    Measured with the same filter the apply uses, so the numbers are what will
    happen rather than an estimate.
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(str(zip_path))

    all_ids = [r.id for r in CLEANUP_RULES]
    tmp = Path(tempfile.mkdtemp(prefix="pg-cleanup-analyze-"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract(zf, tmp)
        root = _find_backup_root(tmp)
        stats = measure_tree(root, set(CLEANABLE_TABLES))
        return {
            "available": True,
            "size": zip_path.stat().st_size,
            "rules": _rule_breakdown(stats, all_ids),
            "default_rule_ids": default_rule_ids(),
            "removable_rows": stats.total_rows,
            "removable_bytes": stats.total_bytes,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_cleanup(
    zip_path: str | Path,
    rule_ids: list[str],
    dest_zip: str | Path,
) -> dict:
    """Write a cleaned copy of `zip_path` to `dest_zip`. Input is never modified."""
    zip_path = Path(zip_path)
    dest_zip = Path(dest_zip)
    if not zip_path.is_file():
        raise FileNotFoundError(str(zip_path))

    tables = resolve_tables(rule_ids)
    applied = [r.id for r in CLEANUP_RULES if r.id in set(rule_ids or ())]

    tmp = Path(tempfile.mkdtemp(prefix="pg-cleanup-apply-"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract(zf, tmp)
        root = _find_backup_root(tmp)
        stats = clean_tree(root, tables) if tables else FilterStats()
        _zip_tree(tmp, dest_zip, tmp)
        return {
            "applied_rule_ids": applied,
            "removed_rows": stats.total_rows,
            "removed_bytes": stats.total_bytes,
            "table_rows": dict(stats.rows),
            "rules": _rule_breakdown(stats, applied),
            "size_before": zip_path.stat().st_size,
            "size_after": dest_zip.stat().st_size,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
