#!/usr/bin/env bash
#
# PGClockMG — PasarGuard restore & migration wizard
#
# Usage:
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh)"
#
# PG_MIGRATOR_PORT=<port>       install non-interactively on that web port
# PG_MIGRATOR_INSTALL_LIB=1     source the helpers only (used by the test suite)
#
set -eo pipefail

readonly SCRIPT_VERSION="3.1.9"
readonly INSTALL_DIR="/opt/pg-migrator"
readonly SERVICE_NAME="pg-migrator"
readonly DEFAULT_WEB_PORT=7000
WEB_PORT="$DEFAULT_WEB_PORT"
PREVIOUS_WEB_PORT=""
readonly TOOLS_DIR="${INSTALL_DIR}/tools"
readonly DEFAULT_REPO="https://github.com/Mrclocks/PGClockMG.git"
readonly DEFAULT_INSTALL_URL="https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh"
readonly REEXEC_MARKER="/tmp/.pg-migrator-reexec"
readonly DEFAULT_BRANCH="main"

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

C_RESET='\033[0m'; C_BOLD='\033[1m'; C_DIM='\033[2m'; C_GREEN='\033[32m'
C_YELLOW='\033[33m'; C_CYAN='\033[36m'; C_RED='\033[31m'; C_WHITE='\033[97m'

log()  { printf '%b\n' "$1"; }
ok()   { log "${C_GREEN}[OK]${C_RESET} $*"; }
info() { log "${C_CYAN}[>>]${C_RESET} $*"; }
warn() { log "${C_YELLOW}[!!]${C_RESET} $*"; }
fail() { log "${C_RED}[ERR]${C_RESET} $*"; exit 1; }

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

# Port of an existing install, so re-running the installer keeps the user's choice.
detect_installed_port() {
  local unit="${1:-/etc/systemd/system/${SERVICE_NAME}.service}" p=""
  [[ -f "$unit" ]] || return 0
  p="$(grep -oE -- '--port[ =]+[0-9]+' "$unit" 2>/dev/null | head -1 | grep -oE '[0-9]+' || true)"
  valid_port "$p" && printf '%s' "$p"
  return 0
}

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

tty_available() { [[ -t 0 || -r /dev/tty ]]; }

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

docker_available() { command -v docker >/dev/null 2>&1; }
docker_running()   { docker info >/dev/null 2>&1; }
compose_available() { docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; }

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

read_app_version() {
  # Prefer version from the synced app (source of truth), not this installer banner alone.
  local v=""
  if [[ -f "${INSTALL_DIR}/app/main.py" ]]; then
    v="$(grep -E '^APP_VERSION\s*=' "${INSTALL_DIR}/app/main.py" | head -1 | sed -E 's/.*["'"'"']([^"'"'"']+)["'"'"'].*/\1/')"
  fi
  if [[ -n "$v" ]]; then
    printf '%s' "$v"
  else
    printf '%s' "$SCRIPT_VERSION"
  fi
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
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
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
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "SERVER_IP")"
  app_ver="$(read_app_version)"
  rm -f "$REEXEC_MARKER"
  log ""
  log "${C_CYAN}${C_BOLD}====================================================${C_RESET}"
  log "${C_WHITE}${C_BOLD}  PGClockMG installed successfully!${C_RESET}"
  log "${C_CYAN}${C_BOLD}====================================================${C_RESET}"
  log ""
  log "  ${C_GREEN}Web panel:${C_RESET}  http://${ip}:${WEB_PORT}"
  log "  ${C_DIM}Version:${C_RESET}    ${app_ver}"
  log "  ${C_DIM}Path:${C_RESET}       ${INSTALL_DIR}"
  log ""
  log "  ${C_YELLOW}Next:${C_RESET} Open the URL above and follow the wizard."
  log "  ${C_DIM}Note:${C_RESET}     This wizard does NOT install PasarGuard — only migrate/restore."
  log ""
}

main() {
  log ""
  log "${C_CYAN}${C_BOLD}  PGClockMG Installer${C_RESET}"
  log "  ${C_DIM}installer script ${SCRIPT_VERSION} — app version shown after sync${C_RESET}"
  log ""
  require_root
  check_ubuntu
  select_web_port
  install_packages
  install_uv
  copy_app_files
  clone_migration_tools
  setup_python_env
  create_systemd_service
  open_firewall
  print_success
}

if [[ "${PG_MIGRATOR_INSTALL_LIB:-0}" != "1" ]]; then
  main "$@"
fi
