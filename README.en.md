> ⚠️ **`v1.11`** — Always take a full backup before restore or migration.

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
- Migrate from Marzban, 3x-ui, Remnawave, and Hiddify
- Panel status guide and official install command

> This wizard does not install PasarGuard. Install the panel yourself first.

---

## Install

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

URL: `http://SERVER_IP:7000`  
Requirements: Ubuntu/Debian · root · Docker

---

## Support

| Panel | Status |
|-------|--------|
| Marzban | Full |
| PasarGuard | Full |
| 3x-ui | Partial |
| Remnawave / Hiddify | Experimental |

---

## Changelog

| Version | Key changes |
|---------|-------------|
| v1.11 | Direct redirect-server install (skip fragile upstream installer) |
| v1.10 | Old 3x-ui links: normalize mapping paths + real redirect port/domain |
| v1.9 | Fix Access denied after 3x-ui→MySQL + correct subscription redirect install |
| v1.8 | Fix 3x-ui → MySQL migration (DB credential probe + core_configs) |
| v1.7 | Fix 3x-ui migration crash on bundle workspace uploads (IsADirectoryError) |
| v1.6beta | Redesigned DB info card, iOS toggle for nodes, new logo |
| v1.5beta | Disable nodes after restore option, DB info card on upload |
| v1.4beta | Fix silent panel restart loop after TimescaleDB restore |
| v1.3beta | Docker image pull, readiness probe, auth fallback after wipe |
| v1.2beta | Fix "role already exists" error during globals.sql restore |
| v1.1beta | Initial public release |

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
