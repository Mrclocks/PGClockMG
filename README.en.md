> ⚠️ **`v3.2.1`** — Always take a full backup before restore or migration.

<p align="center">
  <a href="README.md">فارسی</a> · <b>English</b> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

Web wizard for restore and migration to PasarGuard

</div>

---

## Features

- Restore PasarGuard backups — including database engine changes (SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB)
- Automatic TimescaleDB version mismatch resolution (image pull, readiness probe, auth fallback)
- DB info card after backup upload — compares backup DB vs installed DB, shows compatibility
- Option to keep nodes disabled after restore (avoids conflicts with a still-active previous panel)
- Migrate from Marzban, 3x-ui, Hiddify, and Remnawave
- Panel status guide and official install command

> This wizard does not install PasarGuard. Install the panel yourself first.

---

## Install

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

The script clears the screen and opens a menu:

| Option | What it does |
|--------|--------------|
| 1 · Install / update | Asks for the web panel port (Enter keeps `7000`), installs everything, then prints the panel URL |
| 2 · Uninstall | Removes the service and `/opt/pg-migrator`, offering to keep your backups. PasarGuard, your databases and the redirect server stay untouched |
| 3 · Redirect server | Status, restart, force restart and logs for `pg-redirect` — only used by 3x-ui / Hiddify migrations |
| 4 · Exit | Leaves the menu |

Above the menu the script shows PasarGuard (installed, version, database engine),
Docker, the wizard service and the redirect server. When PasarGuard is missing it
says so — install the panel first, this wizard only migrates data into it.

URL: `http://SERVER_IP:PORT` (default `http://SERVER_IP:7000`)  
Requirements: Ubuntu/Debian · root · Docker · PasarGuard already installed

### Old subscription links broken after a 3x-ui / Hiddify migration?

`pg-redirect` has to bind the port the old panel used. If that panel is still
running it keeps the port and the redirect service cannot start. Run the script,
choose `3` (Redirect server) then `2` (Force restart): it stops the old panel,
frees the ports and brings the redirect server back with a health check.

### Unattended

```bash
sudo PG_MIGRATOR_PORT=8443 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
sudo PG_MIGRATOR_ACTION=redirect-restart bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

`PG_MIGRATOR_ACTION` accepts `install`, `uninstall`, `redirect-restart` or `menu`;
add `PG_MIGRATOR_YES=1` to accept every confirmation.

---

## Support

| Panel | Status |
|-------|--------|
| Marzban | Full |
| PasarGuard | Restore / Change DB (not in panel migrate) |
| 3X-UI | Full |
| Hiddify | Incomplete (users + link redirect) |
| Remnawave | Coming soon |

---

## Commands

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
