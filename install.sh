#!/usr/bin/env bash
#
# PGClockMG — PasarGuard restore & migration wizard
#
# Usage:
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh)"
#
# Interactive menu: install / uninstall / redirect server / exit.
#
# Non-interactive:
#   PG_MIGRATOR_ACTION=install|uninstall|redirect-restart|menu   (or pass as argv[1])
#   PG_MIGRATOR_PORT=<port>       web panel port, no question asked
#   PG_MIGRATOR_YES=1             answer every confirmation with yes
#   PG_MIGRATOR_INSTALL_LIB=1     source the helpers only (used by the test suite)
#
# The test suite also redirects the paths it inspects with PG_MIGRATOR_INSTALL_DIR,
# PG_MIGRATOR_SYSTEMD_DIR, PG_MIGRATOR_PASARGUARD_DIR and PG_MIGRATOR_REDIRECT_DIR.
#
set -eo pipefail

readonly SCRIPT_VERSION="3.2.0"
readonly INSTALL_DIR="${PG_MIGRATOR_INSTALL_DIR:-/opt/pg-migrator}"
readonly SERVICE_NAME="pg-migrator"
readonly SYSTEMD_DIR="${PG_MIGRATOR_SYSTEMD_DIR:-/etc/systemd/system}"
readonly SERVICE_FILE="${SYSTEMD_DIR}/pg-migrator.service"
readonly DEFAULT_WEB_PORT=7000
WEB_PORT="$DEFAULT_WEB_PORT"
PREVIOUS_WEB_PORT=""
readonly TOOLS_DIR="${INSTALL_DIR}/tools"
readonly DEFAULT_REPO="https://github.com/Mrclocks/PGClockMG.git"
readonly DEFAULT_INSTALL_URL="https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh"
readonly REEXEC_MARKER="/tmp/.pg-migrator-reexec"
readonly DEFAULT_BRANCH="main"

readonly PASARGUARD_DIR="${PG_MIGRATOR_PASARGUARD_DIR:-/opt/pasarguard}"
readonly PASARGUARD_ENV="${PASARGUARD_DIR}/.env"
readonly PASARGUARD_DOCS="https://docs.pasarguard.org/en/panel/installation/"

readonly REDIRECT_SERVICE="pg-redirect"
readonly REDIRECT_SERVICE_FILE="${SYSTEMD_DIR}/pg-redirect.service"
readonly REDIRECT_CONFIG_DIR="${PG_MIGRATOR_REDIRECT_DIR:-/etc/pg-redirect}"
readonly REDIRECT_CONFIG="${REDIRECT_CONFIG_DIR}/config.json"
readonly REDIRECT_MAPPING="${REDIRECT_CONFIG_DIR}/mapping.json"

if [[ "${PG_MIGRATOR_INSTALL_LIB:-0}" != "1" ]] && [[ ! -t 0 ]] && [[ ! -f "$REEXEC_MARKER" ]]; then
  tmpfile="$(mktemp /tmp/pg-migrator-install-XXXXXX.sh)"
  cleanup() { rm -f "$tmpfile" "$REEXEC_MARKER"; }
  trap cleanup EXIT
  install_url="${PG_MIGRATOR_INSTALL_URL:-$DEFAULT_INSTALL_URL}"
  curl -fsSL "$install_url" -o "$tmpfile"
  chmod 700 "$tmpfile"
  touch "$REEXEC_MARKER"
  export PG_MIGRATOR_REPO="${PG_MIGRATOR_REPO:-$DEFAULT_REPO}"
  export PG_MIGRATOR_FROM_PIPE=1
  exec bash "$tmpfile" "$@"
fi

set -u

# The re-exec copy owns the marker; drop it (and itself) whenever the menu ends.
if [[ "${PG_MIGRATOR_FROM_PIPE:-0}" == "1" ]]; then
  trap 'rm -f "$REEXEC_MARKER" 2>/dev/null || true' EXIT
fi

# Every long-running probe below is wrapped in `timeout`; keep working without it.
if ! command -v timeout >/dev/null 2>&1; then
  timeout() { shift; "$@"; }
fi

C_RESET='\033[0m'; C_BOLD='\033[1m'; C_DIM='\033[2m'; C_GREEN='\033[32m'
C_YELLOW='\033[33m'; C_CYAN='\033[36m'; C_RED='\033[31m'; C_WHITE='\033[97m'

log()  { printf '%b\n' "$1"; }
ok()   { log "${C_GREEN}[OK]${C_RESET} $*"; }
info() { log "${C_CYAN}[>>]${C_RESET} $*"; }
warn() { log "${C_YELLOW}[!!]${C_RESET} $*"; }
fail() { log "${C_RED}[ERR]${C_RESET} $*"; exit 1; }
# Same message as fail(), but a menu action must not kill the whole script.
fail_soft() { log "${C_RED}[ERR]${C_RESET} $*"; }

require_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "Must run as root: sudo bash install.sh"
}

check_ubuntu() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" || "${ID_LIKE:-}" == *"debian"* ]] || warn "Not Ubuntu/Debian — may have issues"
  fi
}

# ── terminal input ────────────────────────────────────────────────────────────

tty_available() { [[ -t 0 || -r /dev/tty ]]; }

# stdin can be an already-drained pipe (curl | bash), so ask the terminal directly.
ask_tty() {
  local prompt="$1" __var="$2"
  if [[ -t 0 ]]; then
    read -r -p "$prompt" "$__var"
  elif [[ -r /dev/tty ]]; then
    read -r -p "$prompt" "$__var" < /dev/tty
  else
    return 1
  fi
}

confirm() {
  local question="$1" default="${2:-N}" answer hint
  [[ "${PG_MIGRATOR_YES:-0}" == "1" ]] && return 0
  [[ "$default" == "Y" ]] && hint="[Y/n]" || hint="[y/N]"
  if ! tty_available; then
    [[ "$default" == "Y" ]]
    return
  fi
  if ! ask_tty "  ${question} ${hint}: " answer; then
    [[ "$default" == "Y" ]]
    return
  fi
  answer="${answer//[[:space:]]/}"
  answer="${answer,,}"
  [[ -z "$answer" ]] && answer="${default,,}"
  [[ "$answer" == "y" || "$answer" == "yes" ]]
}

# Run a menu action without letting a failure kill the script.
#
# `action || true` looks equivalent but is not: bash disables errexit for the
# whole command whose status is tested, and that state is inherited by the
# functions and subshells it runs — a failing install step would then be
# ignored and the next step would run anyway. Toggling the option around a
# plain call keeps errexit effective inside the action.
LAST_ACTION_RC=0
run_action() {
  set +e
  "$@"
  LAST_ACTION_RC=$?
  set -e
  return 0
}

pause_menu() {
  local _ignored
  tty_available || return 0
  log ""
  ask_tty "  Press Enter to return to the menu... " _ignored || true
}

# ── system probes ─────────────────────────────────────────────────────────────

have_systemd() { command -v systemctl >/dev/null 2>&1; }
docker_available() { command -v docker >/dev/null 2>&1; }
docker_running()   { timeout 15 docker info >/dev/null 2>&1; }
compose_available() { docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; }

unit_active() {
  have_systemd || return 1
  systemctl is-active --quiet "$1" 2>/dev/null
}

unit_enabled() {
  have_systemd || return 1
  [[ "$(systemctl is-enabled "$1" 2>/dev/null || true)" == "enabled" ]]
}

server_ip() {
  local ip=""
  ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}' || true)"
  [[ -z "$ip" ]] && ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  printf '%s' "${ip:-SERVER_IP}"
}

# Last assignment of KEY in a .env style file, quotes stripped.
env_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  grep -E "^[[:space:]]*${key}[[:space:]]*=" "$file" 2>/dev/null \
    | tail -1 \
    | sed -E "s/^[^=]*=[[:space:]]*//; s/^[\"']//; s/[\"'][[:space:]]*$//" \
    || true
}

valid_port() {
  local p="${1:-}"
  [[ "$p" =~ ^[0-9]{1,5}$ ]] || return 1
  (( 10#$p >= 1 && 10#$p <= 65535 ))
}

port_in_use() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$" && return 0
    return 1
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$" && return 0
  fi
  return 1
}

# Kill whatever listens on a port using ss(8) alone (no fuser/lsof needed).
kill_listeners() {
  local p="$1" pids pid
  command -v ss >/dev/null 2>&1 || return 0
  pids="$(timeout 5 ss -lntp 2>/dev/null \
    | grep -E "[:.]${p}[[:space:]]" \
    | grep -oE 'pid=[0-9]+' \
    | grep -oE '[0-9]+' \
    | sort -u || true)"
  for pid in $pids; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    (( pid > 1 )) || continue
    [[ "$pid" == "$$" ]] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
}

port_owner() {
  local p="$1" owner=""
  if command -v ss >/dev/null 2>&1; then
    owner="$(timeout 5 ss -lntp 2>/dev/null | grep -E "[:.]${p}[[:space:]]" | grep -oE 'users:\(\("[^"]+"' | head -1 | grep -oE '"[^"]+"' | tr -d '"' || true)"
  fi
  printf '%s' "$owner"
}

# ── PGClockMG state ───────────────────────────────────────────────────────────

pgclockmg_installed() { [[ -d "$INSTALL_DIR" || -f "$SERVICE_FILE" ]]; }

# Port of an existing install, so re-running the installer keeps the user's choice.
detect_installed_port() {
  local unit="${1:-$SERVICE_FILE}" p=""
  [[ -f "$unit" ]] || return 0
  p="$(grep -oE -- '--port[ =]+[0-9]+' "$unit" 2>/dev/null | head -1 | grep -oE '[0-9]+' || true)"
  valid_port "$p" && printf '%s' "$p"
  return 0
}

read_app_version() {
  # Prefer version from the synced app (source of truth), not this installer banner alone.
  local v=""
  if [[ -f "${INSTALL_DIR}/app/main.py" ]]; then
    v="$(grep -E '^APP_VERSION\s*=' "${INSTALL_DIR}/app/main.py" | head -1 | sed -E 's/.*["'"'"']([^"'"'"']+)["'"'"'].*/\1/' || true)"
  fi
  if [[ -n "$v" ]]; then
    printf '%s' "$v"
  else
    printf '%s' "$SCRIPT_VERSION"
  fi
}

# ── PasarGuard state ──────────────────────────────────────────────────────────

pasarguard_installed() { [[ -d "$PASARGUARD_DIR" && -f "$PASARGUARD_ENV" ]]; }

pasarguard_container() {
  docker_available || return 0
  timeout 8 docker ps --format '{{.Names}}' 2>/dev/null \
    | grep -iE 'pasarguard' \
    | grep -viE 'db|postgres|timescale|mysql|maria|redis|node' \
    | head -1 || true
}

pasarguard_compose_db() {
  local f
  for f in "${PASARGUARD_DIR}/docker-compose.yml" "${PASARGUARD_DIR}/docker-compose.yaml"; do
    [[ -f "$f" ]] || continue
    grep -qiE '^[[:space:]]*image:.*timescale' "$f" && { printf 'timescaledb'; return 0; }
    grep -qiE '^[[:space:]]*image:.*mariadb' "$f" && { printf 'mariadb'; return 0; }
    grep -qiE '^[[:space:]]*image:.*(postgres|pgvector)' "$f" && { printf 'postgresql'; return 0; }
    grep -qiE '^[[:space:]]*image:.*mysql' "$f" && { printf 'mysql'; return 0; }
  done
  return 0
}

# Mirrors app/services/env_migration.detect_db_type_from_env (stamp → URL → compose).
pasarguard_db_type() {
  local stamped url low compose
  pasarguard_installed || return 0
  stamped="$(env_value "$PASARGUARD_ENV" "PASARGUARD_DB_ENGINE" | tr '[:upper:]' '[:lower:]')"
  case "$stamped" in
    sqlite|mysql|mariadb|postgresql|timescaledb) printf '%s' "$stamped"; return 0 ;;
  esac

  url="$(env_value "$PASARGUARD_ENV" "SQLALCHEMY_DATABASE_URL")"
  low="$(printf '%s' "$url" | tr '[:upper:]' '[:lower:]')"
  compose="$(pasarguard_compose_db)"
  case "$low" in
    *sqlite*)    printf 'sqlite'; return 0 ;;
    *mariadb*)   printf 'mariadb'; return 0 ;;
    *timescale*) printf 'timescaledb'; return 0 ;;
  esac
  case "$low" in
    *mysql*|*pymysql*|*asyncmy*)
      case "$compose" in mysql|mariadb) printf '%s' "$compose" ;; *) printf 'mysql' ;; esac
      return 0 ;;
    *postgres*|*asyncpg*)
      case "$compose" in timescaledb|postgresql) printf '%s' "$compose" ;; *) printf 'postgresql' ;; esac
      return 0 ;;
  esac
  [[ -n "$compose" ]] && printf '%s' "$compose"
  return 0
}

# Best effort: image label → image tag → compose file tag.
pasarguard_version() {
  local name="${1:-}" img tag v=""
  if docker_available; then
    [[ -n "$name" ]] || name="$(pasarguard_container)"
    if [[ -n "$name" ]]; then
      v="$(timeout 8 docker inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$name" 2>/dev/null || true)"
      [[ "$v" == "<no value>" ]] && v=""
      if [[ -z "$v" ]]; then
        img="$(timeout 8 docker inspect -f '{{.Config.Image}}' "$name" 2>/dev/null || true)"
        tag="${img##*:}"
        [[ "$tag" == "$img" || "$tag" == */* ]] && tag=""
        v="$tag"
      fi
    fi
  fi
  if [[ -z "$v" ]]; then
    local f
    for f in "${PASARGUARD_DIR}/docker-compose.yml" "${PASARGUARD_DIR}/docker-compose.yaml"; do
      [[ -f "$f" ]] || continue
      # Only a real ":tag" at the end counts — an untagged image has no version.
      v="$(grep -iE '^[[:space:]]*image:.*pasarguard' "$f" 2>/dev/null \
        | head -1 \
        | grep -oE ':[A-Za-z0-9_.+-]+[[:space:]]*$' \
        | tr -d '[:space:]' \
        | sed 's/^://' || true)"
      [[ -n "$v" ]] && break
    done
  fi
  printf '%s' "${v:-unknown}"
}

# ── pg-redirect state ─────────────────────────────────────────────────────────

REDIRECT_PORT=""
REDIRECT_EXTRA_PORTS=""
REDIRECT_PANEL=""
REDIRECT_SSL="false"
REDIRECT_BASE=""

redirect_configured() { [[ -f "$REDIRECT_CONFIG" || -f "$REDIRECT_SERVICE_FILE" ]]; }

redirect_load_config() {
  REDIRECT_PORT=""; REDIRECT_EXTRA_PORTS=""; REDIRECT_PANEL=""
  REDIRECT_SSL="false"; REDIRECT_BASE=""
  [[ -f "$REDIRECT_CONFIG" ]] || return 1

  local parsed="" key value
  if command -v python3 >/dev/null 2>&1; then
    parsed="$(python3 - "$REDIRECT_CONFIG" <<'PY' 2>/dev/null || true
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = json.load(fh)
except Exception:
    raise SystemExit(1)

extras = []
for raw in cfg.get("extra_ports") or []:
    try:
        extras.append(str(int(raw)))
    except (TypeError, ValueError):
        continue

print("PORT=%s" % (cfg.get("port") or ""))
print("EXTRA=%s" % " ".join(extras))
print("PANEL=%s" % (cfg.get("panel") or ""))
print("SSL=%s" % ("true" if (cfg.get("ssl") or {}).get("enabled") else "false"))
print("BASE=%s" % (cfg.get("redirect_base") or ""))
PY
)"
  fi
  if [[ -z "$parsed" ]]; then
    # Fallback for a server without python3 — enough for the status line.
    REDIRECT_PORT="$(grep -oE '"port"[[:space:]]*:[[:space:]]*[0-9]+' "$REDIRECT_CONFIG" 2>/dev/null | head -1 | grep -oE '[0-9]+' || true)"
    REDIRECT_PANEL="$(grep -oE '"panel"[[:space:]]*:[[:space:]]*"[^"]*"' "$REDIRECT_CONFIG" 2>/dev/null | head -1 | sed -E 's/.*"([^"]*)"$/\1/' || true)"
    grep -qE '"enabled"[[:space:]]*:[[:space:]]*true' "$REDIRECT_CONFIG" 2>/dev/null && REDIRECT_SSL="true"
    return 0
  fi

  while IFS='=' read -r key value; do
    case "$key" in
      PORT)  REDIRECT_PORT="$value" ;;
      EXTRA) REDIRECT_EXTRA_PORTS="$value" ;;
      PANEL) REDIRECT_PANEL="$value" ;;
      SSL)   REDIRECT_SSL="$value" ;;
      BASE)  REDIRECT_BASE="$value" ;;
    esac
  done <<< "$parsed"
  return 0
}

redirect_all_ports() {
  local out="${REDIRECT_PORT}"
  [[ -n "$REDIRECT_EXTRA_PORTS" ]] && out="${out} ${REDIRECT_EXTRA_PORTS}"
  printf '%s' "$out"
}

# "443 2083" -> "443, 2083"
format_ports() {
  local p out=""
  for p in ${1:-}; do
    [[ -n "$out" ]] && out="${out}, ${p}" || out="$p"
  done
  printf '%s' "$out"
}

# 0 = healthy, 1 = unhealthy, 2 = could not probe
redirect_healthz() {
  local port="$1" use_ssl="${2:-false}"
  valid_port "$port" || return 2
  command -v python3 >/dev/null 2>&1 || return 2
  timeout 20 python3 - "$port" "$use_ssl" <<'PY' >/dev/null 2>&1
import socket, ssl, sys

port = int(sys.argv[1])
use_ssl = sys.argv[2] == "true"
req = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
try:
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock = raw
    if use_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
    sock.sendall(req)
    data = sock.recv(256).decode("iso-8859-1", "replace")
    sock.close()
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if "200" in data.split("\r\n", 1)[0] else 1)
PY
}

# ── screen ────────────────────────────────────────────────────────────────────

clear_screen() {
  # Never wipe the scrollback when the output is a log file rather than a terminal.
  [[ -t 1 || -r /dev/tty ]] || return 0
  if command -v clear >/dev/null 2>&1; then
    clear || true
  else
    printf '\033[2J\033[H'
  fi
}

rule() { log "${C_DIM}  ────────────────────────────────────────────────────────────${C_RESET}"; }

status_row() {
  local state="$1" label="$2" value="$3" tag color
  case "$state" in
    ok)   tag=" OK " ; color="$C_GREEN"  ;;
    warn) tag="WARN" ; color="$C_YELLOW" ;;
    bad)  tag="MISS" ; color="$C_RED"    ;;
    *)    tag=" -- " ; color="$C_DIM"    ;;
  esac
  printf '   %b[%s]%b  %-17s %b%s%b\n' \
    "$color" "$tag" "$C_RESET" "$label" "$C_WHITE" "$value" "$C_RESET"
}

print_banner() {
  log ""
  log "${C_CYAN}  ╔══════════════════════════════════════════════════════════╗${C_RESET}"
  log "${C_CYAN}  ║${C_RESET}   ${C_BOLD}${C_WHITE}PGClockMG${C_RESET}  ·  PasarGuard restore & migration wizard    ${C_CYAN}║${C_RESET}"
  log "${C_CYAN}  ╚══════════════════════════════════════════════════════════╝${C_RESET}"
}

print_status() {
  local app_ver port ip pg_container pg_ver pg_db redirect_state ports

  ip="$(server_ip)"
  log ""
  log "   ${C_DIM}Installer${C_RESET} ${C_WHITE}${SCRIPT_VERSION}${C_RESET}   ${C_DIM}·${C_RESET}   ${C_DIM}Server${C_RESET} ${C_WHITE}${ip}${C_RESET}"
  log ""
  log "  ${C_BOLD}SYSTEM STATUS${C_RESET}"
  rule

  if pasarguard_installed; then
    pg_container="$(pasarguard_container)"
    pg_ver="$(pasarguard_version "$pg_container")"
    pg_db="$(pasarguard_db_type)"
    if [[ -n "$pg_container" ]]; then
      status_row ok "PasarGuard" "installed · ${pg_ver} · running"
    else
      status_row warn "PasarGuard" "installed · ${pg_ver} · containers stopped"
    fi
    if [[ -n "$pg_db" ]]; then
      status_row ok "Panel database" "$pg_db"
    else
      status_row warn "Panel database" "unknown — check ${PASARGUARD_ENV}"
    fi
  else
    status_row bad "PasarGuard" "not installed"
  fi

  if docker_available && docker_running; then
    status_row ok "Docker" "running"
  elif docker_available; then
    status_row warn "Docker" "installed but not running"
  else
    status_row bad "Docker" "not installed"
  fi

  if pgclockmg_installed; then
    app_ver="$(read_app_version)"
    port="$(detect_installed_port)"
    [[ -z "$port" ]] && port="$DEFAULT_WEB_PORT"
    if unit_active "$SERVICE_NAME"; then
      status_row ok "Wizard" "v${app_ver} · http://${ip}:${port}"
    else
      status_row warn "Wizard" "v${app_ver} · port ${port} · service stopped"
    fi
  else
    status_row none "Wizard" "not installed"
  fi

  if redirect_configured; then
    redirect_load_config || true
    ports="$(format_ports "$(redirect_all_ports)")"
    ports="${ports:-unknown}"
    redirect_state="${REDIRECT_PANEL:-unknown} · port ${ports}"
    if unit_active "$REDIRECT_SERVICE"; then
      status_row ok "Redirect server" "active · ${redirect_state}"
    else
      status_row warn "Redirect server" "stopped · ${redirect_state}"
    fi
  else
    status_row none "Redirect server" "not configured"
  fi

  if ! pasarguard_installed; then
    log ""
    log "   ${C_YELLOW}PasarGuard was not found on this server.${C_RESET}"
    log "   ${C_DIM}This wizard only restores and migrates data INTO an existing panel.${C_RESET}"
    log "   ${C_DIM}Install PasarGuard first: ${PASARGUARD_DOCS}${C_RESET}"
  fi
}

print_menu() {
  log ""
  log "  ${C_BOLD}MENU${C_RESET}"
  rule
  log "   ${C_CYAN}1${C_RESET})  Install / update the web panel"
  log "   ${C_CYAN}2${C_RESET})  Uninstall the web panel"
  log "   ${C_CYAN}3${C_RESET})  Redirect server  ${C_DIM}(3x-ui / Hiddify only)${C_RESET}"
  log "   ${C_CYAN}4${C_RESET})  Exit"
  log ""
}

show_home() {
  clear_screen
  print_banner
  print_status
  print_menu
}

# ── port question ─────────────────────────────────────────────────────────────

select_web_port() {
  local default_port="$DEFAULT_WEB_PORT" answer attempt

  PREVIOUS_WEB_PORT="$(detect_installed_port)"
  [[ -n "$PREVIOUS_WEB_PORT" ]] && default_port="$PREVIOUS_WEB_PORT"

  if [[ -n "${PG_MIGRATOR_PORT:-}" ]]; then
    valid_port "${PG_MIGRATOR_PORT}" \
      || fail "PG_MIGRATOR_PORT='${PG_MIGRATOR_PORT}' is not a valid port (1-65535)"
    WEB_PORT="$((10#${PG_MIGRATOR_PORT}))"
    ok "Web panel port ${WEB_PORT} (from PG_MIGRATOR_PORT)"
    return
  fi

  if ! tty_available; then
    WEB_PORT="$default_port"
    warn "No terminal available for questions — using port ${WEB_PORT} (set PG_MIGRATOR_PORT to change)"
    return
  fi

  log ""
  log "  ${C_BOLD}Which port should the web panel listen on?${C_RESET}"
  [[ -n "$PREVIOUS_WEB_PORT" ]] \
    && log "  ${C_DIM}Current install uses ${PREVIOUS_WEB_PORT}. Press Enter to keep it.${C_RESET}" \
    || log "  ${C_DIM}Press Enter for the default (${DEFAULT_WEB_PORT}).${C_RESET}"

  for attempt in 1 2 3 4 5; do
    answer=""
    if ! ask_tty "  Port [${default_port}]: " answer; then
      WEB_PORT="$default_port"
      warn "Could not read the answer — using port ${WEB_PORT}"
      return
    fi
    answer="${answer//[[:space:]]/}"
    [[ -z "$answer" ]] && answer="$default_port"

    if ! valid_port "$answer"; then
      warn "'${answer}' is not a valid port. Enter a number between 1 and 65535."
      continue
    fi
    answer="$((10#$answer))"

    if [[ "$answer" != "${PREVIOUS_WEB_PORT}" ]] && port_in_use "$answer"; then
      warn "Port ${answer} is already used by another service — pick a different one."
      continue
    fi

    WEB_PORT="$answer"
    ok "Web panel port: ${WEB_PORT}"
    log ""
    return
  done

  fail "No usable port chosen after 5 attempts — re-run with PG_MIGRATOR_PORT=<port>"
}

# ── install ───────────────────────────────────────────────────────────────────

install_packages() {
  info "Installing system packages..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    python3 python3-pip python3-venv curl wget git unzip zip sqlite3 ca-certificates \
    || apt-get install -y \
    python3 python3-pip python3-venv curl wget git unzip zip sqlite3 ca-certificates
  ok "Base packages installed"

  if docker_available; then
    ok "Docker already installed"
    systemctl enable docker >/dev/null 2>&1 || true
    systemctl start docker >/dev/null 2>&1 || true
    docker_running && ok "Docker daemon running" || warn "Docker installed but daemon not running"
  else
    info "Installing Docker..."
    apt-get install -y -qq docker.io 2>/dev/null && ok "docker.io installed" \
      || apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin 2>/dev/null && ok "Docker CE installed" \
      || warn "Docker install failed — continue if already using panels"
    systemctl enable docker >/dev/null 2>&1 || true
    systemctl start docker >/dev/null 2>&1 || true
  fi

  if compose_available; then ok "Docker Compose available"
  else
    apt-get install -y -qq docker-compose-v2 2>/dev/null || apt-get install -y -qq docker-compose 2>/dev/null \
      || warn "Docker Compose not installed"
  fi

  docker_available || warn "Docker missing — Marzban/PasarGuard migrations need Docker"
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then ok "uv already installed"; return; fi
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  [[ -f "${HOME}/.local/bin/uv" ]] && ln -sf "${HOME}/.local/bin/uv" /usr/local/bin/uv 2>/dev/null
  ok "uv installed"
}

copy_app_files() {
  info "Syncing application from GitHub..."
  mkdir -p "$INSTALL_DIR" "$TOOLS_DIR" "${INSTALL_DIR}/uploads" "${INSTALL_DIR}/backups" "${INSTALL_DIR}/logs"

  local repo="${PG_MIGRATOR_REPO:-$DEFAULT_REPO}"
  local branch="${PG_MIGRATOR_BRANCH:-$DEFAULT_BRANCH}"
  rm -rf /tmp/pg-migrator-src
  info "Cloning ${repo} (branch: ${branch})..."
  git clone --depth 1 --branch "$branch" "$repo" /tmp/pg-migrator-src \
    || fail "Could not clone ${repo} @ ${branch}"

  cp -r /tmp/pg-migrator-src/app "${INSTALL_DIR}/"
  cp -f /tmp/pg-migrator-src/requirements.txt "${INSTALL_DIR}/"
  [[ -d /tmp/pg-migrator-src/tests ]] && cp -r /tmp/pg-migrator-src/tests "${INSTALL_DIR}/"
  # Native subscription redirect (stdlib) — must not depend on GitHub downloads at migrate time
  if [[ -d /tmp/pg-migrator-src/tools/pg_redirect ]]; then
    mkdir -p "${TOOLS_DIR}"
    rm -rf "${TOOLS_DIR}/pg_redirect"
    cp -a /tmp/pg-migrator-src/tools/pg_redirect "${TOOLS_DIR}/pg_redirect"
  fi
  rm -rf /tmp/pg-migrator-src

  [[ -f "${INSTALL_DIR}/app/main.py" ]] || fail "Application files not found after sync."
  [[ -f "${TOOLS_DIR}/pg_redirect/__main__.py" ]] || warn "tools/pg_redirect missing — old sub link redirect may fail"
  ok "Application synced to ${INSTALL_DIR}"
}

clone_migration_tools() {
  info "Fetching PasarGuard official migration tools..."
  [[ -d "${TOOLS_DIR}/db-migrations" ]] || git clone --depth 1 https://github.com/PasarGuard/db-migrations.git "${TOOLS_DIR}/db-migrations" 2>/dev/null || warn "db-migrations clone failed"
  [[ -d "${TOOLS_DIR}/migrations" ]] || git clone --depth 1 https://github.com/PasarGuard/migrations.git "${TOOLS_DIR}/migrations" 2>/dev/null || warn "migrations clone failed"
  [[ -d "${TOOLS_DIR}/db-migrations" ]] && command -v uv >/dev/null 2>&1 && (cd "${TOOLS_DIR}/db-migrations" && uv sync 2>/dev/null) || true
  [[ -d "${TOOLS_DIR}/migrations/x-ui" ]] && command -v uv >/dev/null 2>&1 && (cd "${TOOLS_DIR}/migrations/x-ui" && uv sync 2>/dev/null) || true
  ok "Migration tools ready"
}

setup_python_env() {
  info "Setting up Python environment..."
  cd "$INSTALL_DIR"
  python3 -m venv venv
  # shellcheck disable=SC1091
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "Python environment ready"
}

create_systemd_service() {
  info "Creating systemd service..."
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PGClockMG — PasarGuard restore & migration wizard
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=PG_MIGRATOR_HOME=${INSTALL_DIR}
Environment=PG_MIGRATOR_PORT=${WEB_PORT}
Environment=PATH=${INSTALL_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${INSTALL_DIR}/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${WEB_PORT}
Restart=on-failure
RestartSec=5
StandardOutput=append:${INSTALL_DIR}/logs/service.log
StandardError=append:${INSTALL_DIR}/logs/service.log

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  ok "Service ${SERVICE_NAME} started"
}

open_firewall() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "active"; then
    ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true
    ok "Firewall port ${WEB_PORT} opened"
    if [[ -n "$PREVIOUS_WEB_PORT" && "$PREVIOUS_WEB_PORT" != "$WEB_PORT" ]]; then
      ufw delete allow "${PREVIOUS_WEB_PORT}/tcp" >/dev/null 2>&1 || true
      info "Firewall rule for old port ${PREVIOUS_WEB_PORT} removed"
    fi
  fi
}

print_success() {
  local ip app_ver
  ip="$(server_ip)"
  app_ver="$(read_app_version)"
  log ""
  log "${C_GREEN}  ╔══════════════════════════════════════════════════════════╗${C_RESET}"
  log "${C_GREEN}  ║${C_RESET}   ${C_BOLD}${C_WHITE}PGClockMG is installed and running${C_RESET}                     ${C_GREEN}║${C_RESET}"
  log "${C_GREEN}  ╚══════════════════════════════════════════════════════════╝${C_RESET}"
  log ""
  log "   ${C_BOLD}Web panel${C_RESET}   ${C_GREEN}http://${ip}:${WEB_PORT}${C_RESET}"
  log "   ${C_DIM}Port${C_RESET}        ${WEB_PORT}"
  log "   ${C_DIM}Version${C_RESET}     ${app_ver}"
  log "   ${C_DIM}Path${C_RESET}        ${INSTALL_DIR}"
  log "   ${C_DIM}Service${C_RESET}     systemctl status ${SERVICE_NAME}"
  log ""
  log "   ${C_YELLOW}Next:${C_RESET} open the URL above and follow the wizard."
  if ! pasarguard_installed; then
    log "   ${C_YELLOW}Note:${C_RESET} PasarGuard is not installed yet — install the panel before migrating."
  fi
  log ""
}

run_install() {
  check_ubuntu
  install_packages
  install_uv
  copy_app_files
  clone_migration_tools
  setup_python_env
  create_systemd_service
  open_firewall
}

action_install() {
  local rc=0 errexit_was_on=""

  log ""
  log "  ${C_BOLD}INSTALL / UPDATE${C_RESET}"
  rule
  select_web_port
  info "Starting installation — this takes a few minutes."
  log ""

  # Steps run in a subshell so `fail` inside one of them returns here instead
  # of ending the script; `set -e` is re-armed inside it so the install stops
  # at the first failing step (callers go through run_action, see above).
  # Restore — never force — errexit, or returning non-zero would end the script.
  case "$-" in *e*) errexit_was_on=1 ;; esac
  set +e
  ( set -e; run_install )
  rc=$?
  [[ -n "$errexit_was_on" ]] && set -e

  if (( rc == 0 )); then
    print_success
    return 0
  fi
  log ""
  fail_soft "Installation failed. Fix the error above and run the installer again."
  return 1
}

# ── uninstall ─────────────────────────────────────────────────────────────────

action_uninstall() {
  local port backup_dir keep_dir

  log ""
  log "  ${C_BOLD}UNINSTALL${C_RESET}"
  rule

  if ! pgclockmg_installed; then
    warn "PGClockMG is not installed on this server — nothing to remove."
    return 0
  fi

  port="$(detect_installed_port)"
  log "   This removes:"
  log "     ${C_DIM}•${C_RESET} systemd service ${SERVICE_NAME}"
  log "     ${C_DIM}•${C_RESET} ${INSTALL_DIR} (app, uploads, backups, logs)"
  [[ -n "$port" ]] && log "     ${C_DIM}•${C_RESET} firewall rule for port ${port}"
  log ""
  log "   ${C_GREEN}Kept untouched:${C_RESET} PasarGuard, your databases and the redirect server."
  log ""

  confirm "Remove PGClockMG now?" N || { info "Cancelled — nothing was removed."; return 0; }

  backup_dir="${INSTALL_DIR}/backups"
  if [[ -d "$backup_dir" ]] && [[ -n "$(ls -A "$backup_dir" 2>/dev/null || true)" ]]; then
    if confirm "Keep a copy of the backups folder?" Y; then
      keep_dir="${PG_MIGRATOR_BACKUP_DIR:-/root}/pgclockmg-backups-$(date +%Y%m%d-%H%M%S)"
      if mkdir -p "$keep_dir" 2>/dev/null && cp -a "${backup_dir}/." "${keep_dir}/" 2>/dev/null; then
        ok "Backups copied to ${keep_dir}"
      else
        warn "Could not copy the backups to ${keep_dir}"
        confirm "Continue and delete them with the app?" N \
          || { info "Cancelled — nothing was removed."; return 0; }
      fi
    fi
  fi

  if have_systemd; then
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  rm -f "$SERVICE_FILE"
  have_systemd && systemctl daemon-reload >/dev/null 2>&1 || true
  rm -rf "$INSTALL_DIR"

  if [[ -n "$port" ]] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "active"; then
    ufw delete allow "${port}/tcp" >/dev/null 2>&1 || true
    info "Firewall rule for port ${port} removed"
  fi

  ok "PGClockMG removed."
  if redirect_configured; then
    log "   ${C_DIM}The redirect server (${REDIRECT_SERVICE}) is still installed and untouched.${C_RESET}"
  fi
}

# ── redirect server ───────────────────────────────────────────────────────────

redirect_summary() {
  local ports health
  redirect_load_config || true
  ports="$(format_ports "$(redirect_all_ports)")"

  log "   ${C_DIM}Panel${C_RESET}          ${REDIRECT_PANEL:-unknown}"
  log "   ${C_DIM}Listen ports${C_RESET}   ${ports:-unknown}"
  log "   ${C_DIM}TLS${C_RESET}            $([[ "$REDIRECT_SSL" == "true" ]] && echo "enabled" || echo "disabled")"
  [[ -n "$REDIRECT_BASE" ]] && log "   ${C_DIM}Redirects to${C_RESET}   ${REDIRECT_BASE}"
  log "   ${C_DIM}Config${C_RESET}         ${REDIRECT_CONFIG}"
  [[ -f "$REDIRECT_MAPPING" ]] && log "   ${C_DIM}Mapping${C_RESET}        ${REDIRECT_MAPPING}"
  log ""

  if unit_active "$REDIRECT_SERVICE"; then
    ok "Service ${REDIRECT_SERVICE} is active"
  else
    warn "Service ${REDIRECT_SERVICE} is not running"
  fi
  unit_enabled "$REDIRECT_SERVICE" && ok "Enabled at boot" || warn "Not enabled at boot"

  if valid_port "$REDIRECT_PORT"; then
    health=0
    redirect_healthz "$REDIRECT_PORT" "$REDIRECT_SSL" || health=$?
    case "$health" in
      0) ok "Health check passed on port ${REDIRECT_PORT} — old subscription links are being redirected" ;;
      1) warn "Health check failed on port ${REDIRECT_PORT} — use 'Force restart' below" ;;
      *) info "Health check skipped (python3 not available)" ;;
    esac
  fi

  redirect_show_port_owners
}

redirect_show_port_owners() {
  local p owner ports
  ports="$(redirect_all_ports)"
  for p in $ports; do
    valid_port "$p" || continue
    owner="$(port_owner "$p")"
    if [[ -n "$owner" && "$owner" != "python3" ]]; then
      warn "Port ${p} is currently held by '${owner}' — that is usually the old panel"
    fi
  done
}

# Stop whatever still holds the redirect ports (the old panel the user forgot to shut down).
redirect_free_ports() {
  local ports p ids
  ports="$(redirect_all_ports)"

  info "Stopping panels that commonly keep these ports busy..."
  if have_systemd; then
    # Stop our own service first so systemd does not restart it mid-cleanup.
    timeout 20 systemctl stop "$REDIRECT_SERVICE" >/dev/null 2>&1 || true
    timeout 20 systemctl stop x-ui x-ui.service redirect-server >/dev/null 2>&1 || true
  fi

  if [[ "${REDIRECT_PANEL,,}" == "hiddify" ]] || [[ " ${ports} " == *" 443 "* ]]; then
    info "Stopping the Hiddify web stack (panel / nginx / haproxy)..."
    if have_systemd; then
      timeout 30 systemctl disable --now \
        hiddify-panel hiddify-nginx hiddify-haproxy hiddify-gateway \
        nginx haproxy apache2 caddy >/dev/null 2>&1 || true
    fi
  fi

  for p in $ports; do
    valid_port "$p" || continue
    if docker_available; then
      ids="$(timeout 8 docker ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | awk -v pp="$p" 'index($0, ":" pp "->") {print $1}' || true)"
      if [[ -n "$ids" ]]; then
        info "Stopping docker containers publishing :${p}"
        # shellcheck disable=SC2086
        timeout 25 docker stop -t 3 $ids >/dev/null 2>&1 || true
      fi
    fi
    timeout 8 fuser -k "${p}/tcp" >/dev/null 2>&1 || true
    if command -v lsof >/dev/null 2>&1; then
      timeout 8 bash -c "lsof -ti tcp:${p} | xargs -r kill -9" >/dev/null 2>&1 || true
    fi
    # Minimal servers often ship neither psmisc nor lsof, but almost always ss.
    if port_in_use "$p"; then
      kill_listeners "$p"
    fi
  done
  sleep 1
  ok "Redirect ports released"
}

redirect_start_and_verify() {
  local attempt health
  have_systemd || { fail_soft "systemctl is not available on this server."; return 1; }

  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable "$REDIRECT_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$REDIRECT_SERVICE" >/dev/null 2>&1 || true

  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if unit_active "$REDIRECT_SERVICE"; then
      health=0
      redirect_healthz "$REDIRECT_PORT" "$REDIRECT_SSL" || health=$?
      if [[ "$health" == "0" || "$health" == "2" ]]; then
        ok "Redirect server is active on port ${REDIRECT_PORT}"
        [[ "$health" == "2" ]] && info "Health check skipped (python3 not available)"
        [[ "$health" == "0" ]] && ok "Health check passed — old subscription links resolve again"
        return 0
      fi
    fi
    sleep 1
  done

  fail_soft "Redirect server did not come up cleanly."
  log ""
  log "  ${C_DIM}Last log lines:${C_RESET}"
  timeout 15 journalctl -u "$REDIRECT_SERVICE" -n 20 --no-pager 2>/dev/null || true
  redirect_show_port_owners
  return 1
}

action_redirect_cli_restart() {
  if ! redirect_configured; then
    fail_soft "No redirect server is configured — it is created by a 3x-ui / Hiddify migration."
    return 1
  fi
  redirect_load_config || true
  redirect_free_ports
  redirect_start_and_verify
}

action_redirect_restart() {
  log ""
  info "Restarting ${REDIRECT_SERVICE}..."
  redirect_load_config || true
  if redirect_start_and_verify; then
    return 0
  fi
  log ""
  warn "Try 'Force restart' — it stops the old panel that is still holding the port."
  return 1
}

action_redirect_force_restart() {
  local ports
  redirect_load_config || true
  ports="$(redirect_all_ports)"

  log ""
  log "  ${C_BOLD}FORCE RESTART${C_RESET}"
  rule
  log "   Ports to free: ${C_WHITE}$(format_ports "$ports")${C_RESET}"
  log "   This stops anything still listening on them, including:"
  log "     ${C_DIM}•${C_RESET} 3x-ui (x-ui service)"
  log "     ${C_DIM}•${C_RESET} Hiddify web stack (panel, nginx, haproxy) when port 443 is used"
  log "     ${C_DIM}•${C_RESET} docker containers publishing those ports"
  log ""
  log "   ${C_GREEN}PasarGuard, your databases and migrated data are not touched.${C_RESET}"
  log ""

  confirm "Free the ports and restart the redirect server?" Y || { info "Cancelled."; return 0; }

  redirect_free_ports
  redirect_start_and_verify
}

action_redirect_stop() {
  log ""
  warn "Stopping the redirect server means old 3x-ui / Hiddify subscription links stop working."
  confirm "Stop and disable ${REDIRECT_SERVICE}?" N || { info "Cancelled."; return 0; }
  if have_systemd; then
    systemctl disable --now "$REDIRECT_SERVICE" >/dev/null 2>&1 || true
  fi
  if unit_active "$REDIRECT_SERVICE"; then
    fail_soft "Could not stop ${REDIRECT_SERVICE}."
    return 1
  fi
  ok "Redirect server stopped and disabled."
  log "   ${C_DIM}Start it again from this menu at any time.${C_RESET}"
}

action_redirect_logs() {
  log ""
  log "  ${C_BOLD}RECENT REDIRECT LOGS${C_RESET}"
  rule
  if have_systemd; then
    timeout 20 journalctl -u "$REDIRECT_SERVICE" -n 40 --no-pager 2>/dev/null \
      || warn "No logs available for ${REDIRECT_SERVICE}"
  else
    warn "systemctl is not available on this server."
  fi
}

redirect_menu() {
  local choice
  while true; do
    clear_screen
    print_banner
    log ""
    log "  ${C_BOLD}REDIRECT SERVER${C_RESET}  ${C_DIM}(3x-ui / Hiddify migrations only)${C_RESET}"
    rule
    log "   ${C_DIM}Keeps old 3x-ui / Hiddify subscription links alive by redirecting${C_RESET}"
    log "   ${C_DIM}them to PasarGuard. It fails to start when the old panel is still${C_RESET}"
    log "   ${C_DIM}running and holding the same port.${C_RESET}"
    log ""

    if ! redirect_configured; then
      warn "No redirect server is configured on this server."
      log ""
      log "   ${C_DIM}It is created automatically at the end of a 3x-ui or Hiddify${C_RESET}"
      log "   ${C_DIM}migration, when you choose to keep the old subscription links.${C_RESET}"
      pause_menu
      return 0
    fi

    redirect_summary

    log ""
    log "  ${C_BOLD}ACTIONS${C_RESET}"
    rule
    log "   ${C_CYAN}1${C_RESET})  Restart redirect server"
    log "   ${C_CYAN}2${C_RESET})  Force restart  ${C_DIM}(stop the old panel holding the port)${C_RESET}"
    log "   ${C_CYAN}3${C_RESET})  Show recent logs"
    log "   ${C_CYAN}4${C_RESET})  Stop and disable"
    log "   ${C_CYAN}5${C_RESET})  Back to main menu"
    log ""

    choice=""
    if ! ask_tty "  Select an option [1-5]: " choice; then
      return 0
    fi
    choice="${choice//[[:space:]]/}"

    case "$choice" in
      1) run_action action_redirect_restart; pause_menu ;;
      2) run_action action_redirect_force_restart; pause_menu ;;
      3) run_action action_redirect_logs; pause_menu ;;
      4) run_action action_redirect_stop; pause_menu ;;
      5|q|Q|b|B) return 0 ;;
      *) warn "Unknown option '${choice}'"; sleep 1 ;;
    esac
  done
}

# ── menu ──────────────────────────────────────────────────────────────────────

menu_loop() {
  local choice
  while true; do
    show_home
    choice=""
    if ! ask_tty "  Select an option [1-4]: " choice; then
      log ""
      return 0
    fi
    choice="${choice//[[:space:]]/}"

    case "$choice" in
      1) clear_screen; print_banner; run_action action_install; pause_menu ;;
      2) clear_screen; print_banner; run_action action_uninstall; pause_menu ;;
      3) redirect_menu ;;
      4|q|Q|e|E)
        log ""
        log "  ${C_DIM}Bye.${C_RESET}"
        log ""
        return 0
        ;;
      *) warn "Unknown option '${choice}' — choose 1, 2, 3 or 4"; sleep 1 ;;
    esac
  done
}

main() {
  local action="${1:-${PG_MIGRATOR_ACTION:-}}"

  case "$action" in
    install|uninstall|redirect|redirect-restart|menu|"") ;;
    *) fail "Unknown command '${action}' — use: install | uninstall | redirect-restart | menu" ;;
  esac

  require_root

  case "$action" in
    install)
      clear_screen; print_banner
      run_action action_install
      return "$LAST_ACTION_RC"
      ;;
    uninstall)
      clear_screen; print_banner
      run_action action_uninstall
      return "$LAST_ACTION_RC"
      ;;
    redirect-restart|redirect)
      clear_screen; print_banner
      run_action action_redirect_cli_restart
      return "$LAST_ACTION_RC"
      ;;
    *) ;;
  esac

  if ! tty_available; then
    # curl | bash with no terminal attached keeps the classic behaviour: install.
    print_banner
    info "No terminal detected — running an unattended install."
    run_action action_install
    return "$LAST_ACTION_RC"
  fi

  menu_loop
}

if [[ "${PG_MIGRATOR_INSTALL_LIB:-0}" != "1" ]]; then
  main "$@"
fi
