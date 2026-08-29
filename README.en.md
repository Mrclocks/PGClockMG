> 🚀 **`v4.0.0`** — Restore/migration wizard + full PasarGuard backup panel  
> ⚠️ Always take a full backup before restore or migration.

<p align="center">
  <a href="README.md">فارسی</a> · <b>English</b> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

🧰 Restore & migration wizard + PasarGuard backup panel

</div>

---

## ✨ Overview

PGClockMG runs **two services** side by side:

| Service | Default port | Role |
|---------|--------------|------|
| 🧭 **Wizard** (`pg-migrator`) | `7000` | Restore backups and migrate to PasarGuard |
| 💾 **Backup panel** (`pg-backup`) | `7001` | Full backups, Telegram, schedule, stream-to-restore |

One `install.sh` run installs both. There is **no separate menu item** for backup — the installer only asks for each service port.

> 📌 This tool does not install PasarGuard for you. Install the panel first, then come back.

---

## 📦 Requirements

- Ubuntu 22.04+
- `root` access
- Docker
- PasarGuard already installed (for backup and restore)

| URL | Notes |
|-----|--------|
| `http://SERVER_IP:7000/?token=...` | Wizard — **access token** |
| `http://SERVER_IP:7001/` | Backup panel — **strong password** on first visit |

Recover the wizard token:

```bash
cat /opt/pg-migrator/.access_token
```

---

## 📥 Install

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

After running, the script clears the screen and shows the main menu.

### 🗂️ Script menu

| Option | What it does |
|--------|--------------|
| 1 · Install / update | Asks for wizard port (`7000`) and backup port (`7001`), installs/updates both services, prints URLs |
| 2 · Uninstall | Removes services and `/opt/pg-migrator`, offering to keep backups. PasarGuard stays untouched |
| 3 · Redirect server | Status, restart, and logs for `pg-redirect` — 3x-ui / Hiddify only |
| 4 · Exit | Leaves the menu |

### Unattended mode

```bash
sudo PG_MIGRATOR_PORT=7000 PG_BACKUP_PORT=7001 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

`PG_MIGRATOR_ACTION`: `install` · `uninstall` · `redirect-restart` · `menu`  
Add `PG_MIGRATOR_YES=1` to accept every confirmation automatically.

---

## 🧭 Wizard guide (restore & migration)

Default port: **`7000`** · Open with the token URL from the installer.

### Features

- ✅ Restore PasarGuard backups — including DB engine changes (SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB)
- ✅ Auto-handle TimescaleDB version mismatches during restore
- ✅ DB info card after backup upload
- ✅ Optionally keep nodes disabled after restore
- ✅ Migrate from Marzban, 3x-ui, Hiddify, and Remnawave
- ✅ Receive a streamed backup from a source server and restore after **manual confirm**
- ✅ Panel status and official install guidance

### Restore / migration steps

1. Back up your current panel (or use the PGClockMG backup panel).  
2. Install PasarGuard with your chosen database on the **new server**.  
3. Verify the new panel, then temporarily disable the old one.  
4. Install PGClockMG and open the wizard URL (with token).  
5. Run restore or migration step by step.

### Recommended order

1. 📦 Backup the old panel  
2. 🛠️ Install and test PasarGuard on the new server  
3. ⏸️ Temporarily stop the old panel  
4. 🚀 Run restore/migration from the wizard

### Panel support (migration)

| Panel | Status |
|-------|--------|
| Marzban | Full ✅ |
| PasarGuard | Restore / Change DB (not in panel migrate) |
| 3X-UI | Full ✅ |
| Hiddify | Incomplete (users + link redirect) |
| Remnawave | Coming soon |

---

## 💾 Backup panel guide

Default port: **`7001`** · On first visit set a **strong password** (12+ chars, upper/lower, digit, symbol).

### Features

- 📦 **Full-bundle backup** from official PasarGuard paths  
  (`/opt/pasarguard` + `/var/lib/pasarguard`) — restore-compatible with the wizard
- 🗄️ SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB
- 📊 Dashboard: server health, backup disk, live panel stats, last backup
- ⏰ Daily schedule (UTC) + keep last N archives
- 📱 Optional **Telegram** delivery (one file on disk; split only for large uploads)
- 🔌 HTTP/SOCKS proxy for Telegram + **Connected / Disconnected** status tag
- 🌊 **Stream** a backup to a destination wizard → analyze → **manual confirm** → restore
- 🔐 Separate password + session; own port, independent from the wizard

### How to use

1. Open `http://SERVER_IP:7001/` after install.  
2. Create / enter the backup panel password.  
3. From the dashboard run “Create full backup now”, or enable the schedule in Settings.  
4. Optionally configure Telegram + proxy and test the connection.  
5. To move to another server: on the destination wizard start “ready to receive stream”, paste the token into the source backup panel, start the stream — restore runs only after manual confirm.

### Backup notes

- Archives live in `/opt/pg-migrator/backups`
- Backup panel settings live in `/opt/pg-migrator/backup_panel/`
- Backup panel password is separate from the wizard access token

---

## ⚠️ Important notes

### Opening the wizard (access token)

The wizard will not open without a token. Use the installer URL. If you lost it:

```bash
cat /opt/pg-migrator/.access_token
```

Token file mode is `0600` — do not paste it into public chats or screenshots.

### Old subscription links broken after a 3x-ui / Hiddify migration?

`pg-redirect` must bind the old panel port. If that panel is still running, choose script option `3` (Redirect server), then `2` (Force restart).

---

## 🛠️ Useful commands

```bash
# Wizard
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# Backup panel
systemctl status pg-backup
systemctl restart pg-backup
journalctl -u pg-backup -f
# or file log:
tail -f /opt/pg-migrator/logs/backup-service.log

# Update both
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# Stop
systemctl stop pg-migrator pg-backup
systemctl disable pg-migrator pg-backup
```

Pre-`v4.0.0` restore point tag: `restore-point-pre-v4.0.0`

---

## 📄 License

**Copyright (c) 2026 Mrclocks — All rights reserved.**

Personal use on your own server is allowed.  
Copying, republishing, or selling without permission is not allowed.

[`LICENSE`](LICENSE) · [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)
