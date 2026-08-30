> 🚀 **`v4.3.7`** — Update-check button restores fully + correct panel Path
> ⚠️ Always take a full backup before restore or migration.

<p align="center">
  <a href="README.md">فارسی</a> · <b>English</b> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

🧰 Restore/migration wizard + PasarGuard backup panel (separate installs)

</div>

---

## ✨ Overview

| Product | Path | Port | Service |
|---------|------|------|---------|
| 🧭 **PGClockMG** (wizard) | `/opt/pg-migrator` | `7000` | `pg-migrator` |
| 💾 **PGClockBackup** | `/opt/pg-backup` | `7001` | `pg-backup` |

From **v4.0.1** they install and uninstall **independently**. Removing the wizard after restore does **not** remove the backup panel.

> 📌 Install PasarGuard yourself — this tool does not install the panel.

---

## 📥 Install

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

### 🗂️ Script menu

| Option | What it does |
|--------|--------------|
| 1 · Install PGClockMG | Wizard only — asks web port |
| 2 · Install PGClockBackup | Backup only — asks backup port and prints a **one-time setup token** |
| 3 · Uninstall PGClockMG | Removes wizard only (backup stays) |
| 4 · Uninstall PGClockBackup | Removes backup panel only |
| 5 · Redirect server | 3x-ui / Hiddify |
| 6 · Exit | Leave menu |

### Unattended

```bash
sudo PG_MIGRATOR_ACTION=install-wizard PG_MIGRATOR_PORT=7000 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
sudo PG_MIGRATOR_ACTION=install-backup PG_BACKUP_PORT=7001 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

---

## 🧭 Wizard guide (PGClockMG)

URL: `http://SERVER_IP:7000/?token=...`

### Features
- ✅ Restore PasarGuard backups (including DB engine changes)
- ✅ Migrate Marzban / 3x-ui / Hiddify / Remnawave
- ✅ Receive streamed backup → **manual confirm** → restore
- ✅ TimescaleDB auto-heal

After a successful restore you can **Uninstall PGClockMG**; PGClockBackup keeps running if installed.

```bash
cat /opt/pg-migrator/.access_token
```

---

## 💾 Backup panel guide (PGClockBackup)

URL: `http://SERVER_IP:7001/` · First visit: **setup token** + strong password

### Features
- 📦 Full bundle from `/opt/pasarguard` + `/var/lib/pasarguard`
- 🗄️ SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB
- 📊 Health dashboard & live stats
- ⏰ Daily schedule + keep last N
- 📱 Telegram (+ proxy) with Connected/Disconnected tag
- 🌊 Stream to destination wizard → manual confirm → restore
- 🔐 Separate password, session, and install path

### Security highlights
- One-time setup token for first password
- Login throttling
- Session rotation on password change
- SSRF blocks on stream destinations / abusive proxy hosts
- No public OpenAPI on the backup app

```bash
cat /opt/pg-backup/backup_panel/.setup_token
```

---

## 🛠️ Commands

```bash
systemctl status pg-migrator
systemctl status pg-backup
journalctl -u pg-migrator -f
journalctl -u pg-backup -f
```

Pre-v4 restore point: `restore-point-pre-v4.0.0`

---

## 📄 License

**Copyright (c) 2026 Mrclocks — All rights reserved.**

[`LICENSE`](LICENSE) · [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)
