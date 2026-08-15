> ⚠️ **`v3.1.2`** — Always take a full backup before restore or migration.

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

URL: `http://SERVER_IP:7000`  
Requirements: Ubuntu/Debian · root · Docker

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
