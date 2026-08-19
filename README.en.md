> ⚠️ **`v3.2.3`** — Always take a full backup before restore or migration.

<p align="center">
  <a href="README.md">فارسی</a> · <b>English</b> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

Web wizard for restore and migration to PasarGuard

</div>

---

## Overview

PGClockMG is a web wizard for **backup restore** and **migration to PasarGuard**.

### What it can do

- Restore PasarGuard backups — including database engine changes (SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB)
- Automatically handle TimescaleDB version mismatches during restore (image pull, readiness probe, auth fallback)
- Show a DB info card after backup upload — compares backup DB vs installed DB and shows compatibility
- Optionally keep nodes disabled after restore to avoid conflicts with a still-active previous panel
- Migrate from Marzban, 3x-ui, Hiddify, and Remnawave
- Show panel status and official installation guidance

> This wizard does not install PasarGuard for you. Install the panel first, then come back.

---

## Requirements

- Ubuntu 22.04+
- `root` access
- Docker
- PasarGuard already installed on the new server

Panel URL after install: `http://SERVER_IP:PORT`  
Default port: `7000`

---

## Install

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

After running, the script clears the screen and shows the main menu.

### Script menu

| Option | What it does |
|--------|--------------|
| 1 · Install / update | Asks for the web panel port (Enter keeps `7000`), completes installation, then prints the login URL |
| 2 · Uninstall | Removes the service and `/opt/pg-migrator`, offering to keep your backups. PasarGuard, your databases, and the redirect server stay untouched |
| 3 · Redirect server | Status, restart, force restart, and logs for `pg-redirect` — only used by 3x-ui / Hiddify migrations |
| 4 · Exit | Leaves the menu |

Above the menu the script shows PasarGuard status (installed, version, database
engine), Docker, the wizard service, and the redirect server. If PasarGuard is
missing, the script says so — it only moves data into an existing panel.

---

## How to use

### Main workflow

1. Take a backup from your current panel.  
2. Install PasarGuard with the database engine you want on the **new server**. The
   old panel's database type does not matter; if it is different, the wizard runs
   the required conversion/migration automatically.  
3. Make sure the new panel is up and reachable, then temporarily disable the old panel.  
4. Install the PGClockMG script on the new server.  
5. At the end of installation the script prints the web panel address. Open it and follow the wizard step by step.  

### Recommended order

- First, back up the old panel
- Then install and test PasarGuard on the new server
- Temporarily disable the old panel so links, ports, or nodes do not conflict
- Finally, run restore or migration from the PGClockMG web panel

---

## Important notes

### Old subscription links broken after a 3x-ui / Hiddify migration?

`pg-redirect` has to bind the port the old panel used. If that panel is still
running, it keeps the port and the redirect service cannot start. Run the script,
choose `3` (Redirect server), then `2` (Force restart): it stops the old panel,
frees the ports, and brings the redirect server back with a health check.

### Unattended mode

```bash
sudo PG_MIGRATOR_PORT=8443 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
sudo PG_MIGRATOR_ACTION=redirect-restart bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

`PG_MIGRATOR_ACTION` accepts `install`, `uninstall`, `redirect-restart`, or `menu`  
Add `PG_MIGRATOR_YES=1` to accept every confirmation automatically.

---

## Panel support

| Panel | Status |
|-------|--------|
| Marzban | Full |
| PasarGuard | Restore / Change DB (not in panel migrate) |
| 3X-UI | Full |
| Hiddify | Incomplete (users + link redirect) |
| Remnawave | Coming soon |

---

## Useful commands

```bash
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# Update
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# Stop
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## License

**Copyright (c) 2026 Mrclocks — All rights reserved.**

Personal use on your own server is allowed.  
Copying, republishing, or selling without permission is not allowed.

[`LICENSE`](LICENSE) · [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)
