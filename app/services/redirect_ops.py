"""Install and manage the native PGClockMG pg-redirect service (no GitHub downloads)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import BASE_DIR, TOOLS_DIR

# POSIX paths as strings so Windows-hosted unit tests don't rewrite separators
INSTALL_ROOT = "/opt/pg-redirect"
CONFIG_DIR = "/etc/pg-redirect"
SERVICE_NAME = "pg-redirect"
SERVICE_FILE = f"/etc/systemd/system/{SERVICE_NAME}.service"
SERVICE_USER = "pgredirect"


def bundled_pg_redirect_src() -> Path | None:
    """Locate tools/pg_redirect shipped with the wizard."""
    candidates = [
        TOOLS_DIR / "pg_redirect",
        BASE_DIR / "tools" / "pg_redirect",
        Path(__file__).resolve().parents[2] / "tools" / "pg_redirect",
    ]
    for path in candidates:
        if (path / "__main__.py").is_file() and (path / "server.py").is_file():
            return path
    return None


def _ensure_pg_redirect_importable(src: Path | None = None) -> None:
    root = (src or bundled_pg_redirect_src())
    if not root:
        return
    parent = str(root.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)


def build_runtime_config(
    *,
    listen_port: int,
    redirect_base: str,
    panel: str = "x-ui",
    ssl_cert: str = "",
    ssl_key: str = "",
) -> dict:
    cert_pem = ""
    key_pem = ""
    if ssl_cert and ssl_key and Path(ssl_cert).is_file() and Path(ssl_key).is_file():
        cert_pem = Path(ssl_cert).read_text(encoding="utf-8", errors="ignore")
        key_pem = Path(ssl_key).read_text(encoding="utf-8", errors="ignore")
    elif ssl_cert and ssl_key and "BEGIN" in ssl_cert:
        cert_pem = ssl_cert
        key_pem = ssl_key

    _ensure_pg_redirect_importable()
    try:
        from pg_redirect.config import build_config_dict

        return build_config_dict(
            listen_port=listen_port,
            redirect_base=redirect_base,
            panel=panel,
            ssl_cert_pem=cert_pem,
            ssl_key_pem=key_pem,
        )
    except Exception:
        ssl_enabled = bool(cert_pem and key_pem)
        base = (redirect_base or "").rstrip("/")
        return {
            "host": "0.0.0.0",
            "port": int(listen_port),
            "redirect_base": base,
            "redirect_domain": base,
            "panel": panel,
            "ssl": {
                "enabled": ssl_enabled,
                "cert": cert_pem if ssl_enabled else "",
                "key": key_pem if ssl_enabled else "",
            },
        }


def _systemd_unit(user: str = SERVICE_USER, group: str | None = None) -> str:
    grp = group or user
    return f"""[Unit]
Description=PGClockMG subscription URL redirect (pg-redirect)
After=network.target

[Service]
Type=simple
User={user}
Group={grp}
WorkingDirectory={INSTALL_ROOT}
Environment=PYTHONPATH={INSTALL_ROOT}
ExecStart=/usr/bin/python3 -m pg_redirect --config {CONFIG_DIR}/config.json --map {CONFIG_DIR}/mapping.json
Restart=on-failure
RestartSec=3s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _install_user_snippet() -> str:
    """Bash that resolves a runtime user without requiring useradd on PATH."""
    return f'''
# Prefer dedicated system user; fall back when useradd/adduser missing (minimal images).
_nologin_shell() {{
  for s in /usr/sbin/nologin /sbin/nologin /bin/false; do
    [[ -x "$s" ]] && {{ echo "$s"; return; }}
  done
  echo /bin/false
}}
_try_create_user() {{
  local u="$1" home="$2" sh
  sh="$(_nologin_shell)"
  id "$u" >/dev/null 2>&1 && return 0
  if [[ -x /usr/sbin/useradd ]]; then
    /usr/sbin/useradd --system --home "$home" --shell "$sh" "$u" && return 0
  fi
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --home "$home" --shell "$sh" "$u" && return 0
  fi
  if command -v adduser >/dev/null 2>&1; then
    # Debian/Ubuntu
    adduser --system --home "$home" --shell "$sh" --no-create-home --group "$u" 2>/dev/null && return 0
    # Alpine / BusyBox
    adduser -S -D -H -h "$home" -s "$sh" "$u" 2>/dev/null && return 0
  fi
  return 1
}}
_resolve_svc_group() {{
  local u="$1"
  if [[ "$u" == "nobody" ]]; then
    if getent group nogroup >/dev/null 2>&1; then echo nogroup; return; fi
    if getent group nobody >/dev/null 2>&1; then echo nobody; return; fi
  fi
  if getent group "$u" >/dev/null 2>&1; then echo "$u"; return; fi
  id -gn "$u" 2>/dev/null || echo "$u"
}}
SVC_USER="{SERVICE_USER}"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  _try_create_user "$SVC_USER" "{INSTALL_ROOT}" || true
fi
if ! id "$SVC_USER" >/dev/null 2>&1; then
  if id nobody >/dev/null 2>&1; then
    SVC_USER=nobody
    echo "pg-redirect: using nobody (useradd/adduser unavailable)"
  else
    SVC_USER=root
    echo "pg-redirect: using root (no unprivileged user available)"
  fi
fi
SVC_GROUP="$(_resolve_svc_group "$SVC_USER")"
echo "pg-redirect runtime user=$SVC_USER group=$SVC_GROUP"
'''


async def free_listen_port(migrator, port: int) -> None:
    """Best-effort: stop x-ui / legacy redirect and free the subscription port."""
    await migrator._run_cmd(
        ["bash", "-c", "systemctl stop x-ui 2>/dev/null || true"],
        timeout=60,
    )
    await migrator._run_cmd(
        ["bash", "-c", "systemctl stop x-ui.service 2>/dev/null || true"],
        timeout=60,
    )
    await migrator._run_cmd(
        ["bash", "-c", "systemctl stop redirect-server 2>/dev/null || true"],
        timeout=30,
    )
    await migrator._run_cmd(
        [
            "bash", "-c",
            f"fuser -k {int(port)}/tcp 2>/dev/null || "
            f"(command -v lsof >/dev/null && "
            f"lsof -ti tcp:{int(port)} | xargs -r kill -9) || true",
        ],
        timeout=30,
    )
    migrator.job.log(f"Freed port {port} for pg-redirect")


async def install_pg_redirect(
    migrator,
    mapping_file: Path,
    *,
    listen_port: int,
    redirect_base: str,
    panel: str = "x-ui",
    ssl_cert: str = "",
    ssl_key: str = "",
) -> tuple[bool, str]:
    """Install/update native pg-redirect from bundled sources. No network required."""
    mapping = Path(mapping_file)
    if not mapping.is_file():
        return False, "mapping file missing"

    src = bundled_pg_redirect_src()
    if not src:
        return False, "bundled tools/pg_redirect not found — reinstall PGClockMG"

    _ensure_pg_redirect_importable(src)
    try:
        from pg_redirect.mapping import normalize_mapping_file

        normalize_mapping_file(mapping, redirect_base=redirect_base)
    except Exception as e:
        migrator.job.log(f"mapping normalize note: {e}")

    cfg = build_runtime_config(
        listen_port=listen_port,
        redirect_base=redirect_base,
        panel=panel,
        ssl_cert=ssl_cert,
        ssl_key=ssl_key,
    )
    work_cfg = mapping.parent / "pg-redirect-config.json"
    work_cfg.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    ssl_on = bool(cfg.get("ssl", {}).get("enabled"))
    migrator.job.log(
        f"pg-redirect config: port={listen_port} redirect_base={redirect_base} ssl={ssl_on}"
    )

    await free_listen_port(migrator, listen_port)

    unit_path = mapping.parent / "pg-redirect.service"
    # Unit is finalized on the server after resolving the runtime user.
    unit_path.write_text(_systemd_unit("__USER__", "__GROUP__"), encoding="utf-8")

    ssl_py = "True" if ssl_on else "False"
    user_snippet = _install_user_snippet()
    script = f'''set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then
  echo "pg-redirect install requires root" >&2
  exit 1
fi
command -v python3 >/dev/null || {{ echo "python3 missing" >&2; exit 1; }}
command -v systemctl >/dev/null || {{ echo "systemctl missing" >&2; exit 1; }}

SRC="{src}"
install -d -m 0755 "{INSTALL_ROOT}"
rm -rf "{INSTALL_ROOT}/pg_redirect"
cp -a "$SRC" "{INSTALL_ROOT}/pg_redirect"
test -f "{INSTALL_ROOT}/pg_redirect/__main__.py"

{user_snippet}

install -d -m 0750 "{CONFIG_DIR}"
cp -f "{mapping}" "{CONFIG_DIR}/mapping.json"
cp -f "{work_cfg}" "{CONFIG_DIR}/config.json"
chown -R "$SVC_USER:$SVC_GROUP" "{INSTALL_ROOT}" "{CONFIG_DIR}"
chmod 0640 "{CONFIG_DIR}/mapping.json" "{CONFIG_DIR}/config.json"

sed -e "s/__USER__/${{SVC_USER}}/g" -e "s/__GROUP__/${{SVC_GROUP}}/g" \\
  "{unit_path}" > "{SERVICE_FILE}"
chmod 0644 "{SERVICE_FILE}"

systemctl disable --now redirect-server 2>/dev/null || true
fuser -k {int(listen_port)}/tcp 2>/dev/null || true
systemctl daemon-reload
systemctl enable {SERVICE_NAME}
systemctl restart {SERVICE_NAME}
sleep 1
if ! systemctl is-active --quiet {SERVICE_NAME}; then
  echo "pg-redirect failed to start:" >&2
  systemctl status {SERVICE_NAME} --no-pager -l || true
  journalctl -u {SERVICE_NAME} -n 50 --no-pager || true
  ss -lntp "sport = :{int(listen_port)}" 2>/dev/null || true
  exit 1
fi

python3 - <<PY
import socket, ssl, sys
port = {int(listen_port)}
ssl_on = {ssl_py}
req = b"GET /healthz HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\nConnection: close\\r\\n\\r\\n"
try:
    raw = socket.create_connection(("127.0.0.1", port), timeout=3)
    sock = raw
    if ssl_on:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
    sock.sendall(req)
    data = sock.recv(256).decode("iso-8859-1", "replace")
    sock.close()
    if "200" not in data.split("\\r\\n", 1)[0]:
        print("healthz bad response:", data[:120], file=sys.stderr)
        sys.exit(1)
    print("pg-redirect healthz ok")
except Exception as e:
    print("healthz failed:", e, file=sys.stderr)
    sys.exit(1)
PY

echo "pg-redirect active on port {int(listen_port)} as $SVC_USER"
'''

    ok, out = await migrator._run_cmd(["bash", "-c", script], timeout=180)
    if ok:
        migrator.job.log(
            "pg-redirect installed — old subscription paths → PasarGuard /sub/{token}"
        )
        return True, ""
    detail = (out or "")[-800:]
    migrator.job.log(f"pg-redirect install failed: {detail}")
    return False, detail


async def pg_redirect_is_active(migrator) -> bool:
    _ok, out = await migrator._run_cmd(
        ["bash", "-c", f"systemctl is-active {SERVICE_NAME} 2>/dev/null || true"],
        timeout=15,
    )
    return "active" in (out or "").split()
