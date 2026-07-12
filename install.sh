#!/usr/bin/env bash
#
# PG-Migrator — PasarGuard Panel Migration Wizard
#
# Usage:
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh)"
#
set -eo pipefail

readonly SCRIPT_VERSION="1.7.1"
readonly INSTALL_DIR="/opt/pg-migrator"
readonly SERVICE_NAME="pg-migrator"
readonly WEB_PORT=7000
readonly TOOLS_DIR="${INSTALL_DIR}/tools"
readonly DEFAULT_REPO="https://github.com/Mrclocks/PGClockMG.git"
readonly DEFAULT_INSTALL_URL="https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh"
readonly REEXEC_MARKER="/tmp/.pg-migrator-reexec"

if [[ ! -t 0 ]] && [[ ! -f "$REEXEC_MARKER" ]]; then
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
  rm -rf /tmp/pg-migrator-src
  git clone --depth 1 --branch main "$repo" /tmp/pg-migrator-src \
    || fail "Could not clone ${repo}"

  cp -r /tmp/pg-migrator-src/app "${INSTALL_DIR}/"
  cp -f /tmp/pg-migrator-src/requirements.txt "${INSTALL_DIR}/"
  [[ -d /tmp/pg-migrator-src/tests ]] && cp -r /tmp/pg-migrator-src/tests "${INSTALL_DIR}/"
  rm -rf /tmp/pg-migrator-src

  [[ -f "${INSTALL_DIR}/app/main.py" ]] || fail "Application files not found after sync."
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
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=PG-Migrator — PasarGuard Migration Wizard
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=PG_MIGRATOR_HOME=${INSTALL_DIR}
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
  fi
}

print_success() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "SERVER_IP")"
  rm -f "$REEXEC_MARKER"
  log ""
  log "${C_CYAN}${C_BOLD}====================================================${C_RESET}"
  log "${C_WHITE}${C_BOLD}  PG-Migrator installed successfully!${C_RESET}"
  log "${C_CYAN}${C_BOLD}====================================================${C_RESET}"
  log ""
  log "  ${C_GREEN}Web panel:${C_RESET}  http://${ip}:${WEB_PORT}"
  log "  ${C_DIM}Version:${C_RESET}    ${SCRIPT_VERSION}"
  log "  ${C_DIM}Path:${C_RESET}       ${INSTALL_DIR}"
  log ""
  log "  ${C_YELLOW}Next:${C_RESET} Open the URL above and follow the wizard."
  log ""
}

main() {
  log ""
  log "${C_CYAN}${C_BOLD}  PG-Migrator Installer v${SCRIPT_VERSION}${C_RESET}"
  log ""
  require_root
  check_ubuntu
  install_packages
  install_uv
  copy_app_files
  clone_migration_tools
  setup_python_env
  create_systemd_service
  open_firewall
  print_success
}

main "$@"
