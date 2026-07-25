> ⚠️ **Beta `v1.1beta`** — experimental. Always take a full backup before restore or migration.

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

- Restore PasarGuard backups (including database engine changes)
- Migrate from Marzban, 3x-ui, Remnawave, and Hiddify
- Panel status and official install guide

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
