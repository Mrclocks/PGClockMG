#!/usr/bin/env bash
#
# PG-Migrator — PasarGuard Panel Migration Wizard
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh | sudo bash
#   # or (recommended):
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh)"
#
set -eo pipefail

readonly SCRIPT_VERSION="1.0.1"
readonly INSTALL_DIR="/opt/pg-migrator"
readonly SERVICE_NAME="pg-migrator"
readonly WEB_PORT=7000
readonly TOOLS_DIR="${INSTALL_DIR}/tools"
readonly DEFAULT_REPO="https://github.com/Mrclocks/PGClockMG.git"
readonly DEFAULT_INSTALL_URL="https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh"
readonly REEXEC_MARKER="/tmp/.pg-migrator-reexec"

# When piped via curl, stdin is not a tty — re-exec from a real file on disk.
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

# Safe to enable nounset after re-exec guard
set -u

C_RESET='\033[0m'; C_BOLD='\033[1m'; C_DIM='\033[2m'; C_GREEN='\033[32m'
C_YELLOW='\033[33m'; C_CYAN='\033[36m'; C_RED='\033[31m'; C_WHITE='\033[97m'

log()  { printf '%b\n' "$1"; }
ok()   { log "${C_GREEN}[✓]${C_RESET} $*"; }
info() { log "${C_CYAN}[→]${C_RESET} $*"; }
warn() { log "${C_YELLOW}[!]${C_RESET} $*"; }
fail() { log "${C_RED}[✗]${C_RESET} $*"; exit 1; }

require_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "این اسکریپت باید با root اجرا شود: sudo bash install.sh"
}

check_ubuntu() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" || "${ID_LIKE:-}" == *"debian"* ]] || warn "سیستم‌عامل Ubuntu/Debian نیست — ممکن است مشکلاتی پیش بیاید"
  fi
}

install_packages() {
  info "نصب پیش‌نیازهای سیستم..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    python3 python3-pip python3-venv \
    curl wget git unzip zip \
    docker.io docker-compose-v2 \
    sqlite3 \
    ca-certificates \
    > /dev/null 2>&1 || apt-get install -y \
    python3 python3-pip python3-venv \
    curl wget git unzip zip \
    docker.io docker-compose \
    sqlite3 ca-certificates

  systemctl enable docker >/dev/null 2>&1 || true
  systemctl start docker >/dev/null 2>&1 || true
  ok "پیش‌نیازها نصب شدند"
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    ok "uv از قبل نصب است"
    return
  fi
  info "نصب uv (مدیر پکیج Python)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  if [[ -f "${HOME}/.local/bin/uv" ]]; then
    ln -sf "${HOME}/.local/bin/uv" /usr/local/bin/uv 2>/dev/null || cp "${HOME}/.local/bin/uv" /usr/local/bin/uv
  fi
  ok "uv نصب شد"
}

copy_app_files() {
  info "کپی فایل‌های برنامه..."
  mkdir -p "$INSTALL_DIR" "$TOOLS_DIR" "${INSTALL_DIR}/uploads" "${INSTALL_DIR}/backups" "${INSTALL_DIR}/logs"

  # Local install: copy from script directory (only when run as a real file, not pipe)
  if [[ -z "${PG_MIGRATOR_FROM_PIPE:-}" ]] && [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -d "${script_dir}/app" ]]; then
      mkdir -p "${INSTALL_DIR}/app"
      cp -r "${script_dir}/app/." "${INSTALL_DIR}/app/"
      cp -f "${script_dir}/requirements.txt" "${INSTALL_DIR}/" 2>/dev/null || true
    fi
  fi

  # curl install or missing app: clone from GitHub
  if [[ ! -f "${INSTALL_DIR}/app/main.py" ]]; then
    local repo="${PG_MIGRATOR_REPO:-$DEFAULT_REPO}"
    info "دریافت سورس از GitHub..."
    rm -rf /tmp/pg-migrator-src
    git clone --depth 1 "$repo" /tmp/pg-migrator-src
    cp -r /tmp/pg-migrator-src/. "$INSTALL_DIR/"
    rm -rf /tmp/pg-migrator-src
  fi

  [[ -f "${INSTALL_DIR}/app/main.py" ]] || fail "فایل‌های برنامه یافت نشد."
  ok "فایل‌ها کپی شدند به ${INSTALL_DIR}"
}

clone_migration_tools() {
  info "دریافت ابزارهای رسمی مهاجرت PasarGuard..."

  if [[ ! -d "${TOOLS_DIR}/db-migrations" ]]; then
    git clone --depth 1 https://github.com/PasarGuard/db-migrations.git "${TOOLS_DIR}/db-migrations" 2>/dev/null || \
      warn "کلون db-migrations ناموفق — بعداً دوباره تلاش می‌شود"
  fi

  if [[ ! -d "${TOOLS_DIR}/migrations" ]]; then
    git clone --depth 1 https://github.com/PasarGuard/migrations.git "${TOOLS_DIR}/migrations" 2>/dev/null || \
      warn "کلون migrations ناموفق — بعداً دوباره تلاش می‌شود"
  fi

  if [[ -d "${TOOLS_DIR}/db-migrations" ]] && command -v uv >/dev/null 2>&1; then
    (cd "${TOOLS_DIR}/db-migrations" && uv sync 2>/dev/null) || true
  fi

  if [[ -d "${TOOLS_DIR}/migrations/x-ui" ]] && command -v uv >/dev/null 2>&1; then
    (cd "${TOOLS_DIR}/migrations/x-ui" && uv sync 2>/dev/null) || true
  fi

  ok "ابزارهای مهاجرت آماده شدند"
}

setup_python_env() {
  info "راه‌اندازی محیط Python..."
  cd "$INSTALL_DIR"

  python3 -m venv venv
  # shellcheck disable=SC1091
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  ok "محیط Python آماده شد"
}

create_systemd_service() {
  info "ایجاد سرویس systemd..."
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
  ok "سرویس ${SERVICE_NAME} فعال شد"
}

open_firewall() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "active"; then
    ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true
    ok "پورت ${WEB_PORT} در فایروال باز شد"
  fi
}

print_success() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "SERVER_IP")"
  rm -f "$REEXEC_MARKER"
  log ""
  log "${C_CYAN}${C_BOLD}════════════════════════════════════════════════════${C_RESET}"
  log "${C_WHITE}${C_BOLD}  PG-Migrator با موفقیت نصب شد!${C_RESET}"
  log "${C_CYAN}${C_BOLD}════════════════════════════════════════════════════${C_RESET}"
  log ""
  log "  ${C_GREEN}وب‌پنل مهاجرت:${C_RESET}  http://${ip}:${WEB_PORT}"
  log "  ${C_DIM}نسخه:${C_RESET}            ${SCRIPT_VERSION}"
  log "  ${C_DIM}مسیر نصب:${C_RESET}       ${INSTALL_DIR}"
  log ""
  log "  ${C_YELLOW}مراحل بعدی:${C_RESET}"
  log "    1. مرورگر را باز کنید و به آدرس بالا بروید"
  log "    2. پنل مبدأ را انتخاب کنید"
  log "    3. مهاجرت را شروع کنید"
  log ""
  log "  ${C_DIM}دستورات مفید:${C_RESET}"
  log "    systemctl status ${SERVICE_NAME}"
  log "    systemctl restart ${SERVICE_NAME}"
  log "    journalctl -u ${SERVICE_NAME} -f"
  log ""
}

main() {
  log ""
  log "${C_CYAN}${C_BOLD}  PG-Migrator Installer v${SCRIPT_VERSION}${C_RESET}"
  log "${C_DIM}  PasarGuard Panel Migration Wizard${C_RESET}"
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
