> ⚠️ **`v2.0.7`** — Always take a full backup before restore or migration.

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
| PasarGuard | Restore / Change DB (not in panel migrate) |
| 3X-UI | Full |
| Remnawave | Coming soon |
| Hiddify | Incomplete (users + link redirect) |

---

## Changelog

| Version | Key changes |
|---------|-------------|
| v2.0.7 | Enable partial Hiddify migrate: same UUID users + old link redirect |
| v2.0.6 | Extend panel-boot health wait while Alembic is still running (avoid early ephemeral MySQL deletion) |
| v2.0.5 | Fix empty-JSON error on migrate start + block concurrent migrations (staging port races) |
| v2.0.4 | Fix Marzban MySQL→Timescale when source alembic_version is missing from PasarGuard (staging-only heal) |
| v2.0.3 | Fix Timescale restore: detect old chunk.schema_name catalog and pin image to 2.28.3 on 2.29+ hosts |
| v2.0.2 | Fix MariaDB→Timescale migrate (ephemeral CREATE DATABASE + stage on mariadb:11) |
| v2.0.1 | Panel/sub redirect prefers PasarGuard domain, else IP; tracks later .env/domain changes |
| v2.0 | Full 3X-UI migrate (auto old/new schema, Host/Admin/Group, redirect path/port) |
| v1.16.3 | Fix /sub Internal Server Error (short 3x-ui shadowsocks passwords) |
| v1.16.2 | Auto-detect 3x-ui schema + seed Hosts/Admin so /sub works for modern panels |
| v1.16.1 | Full modern 3x-ui (multi-inbound) migrate + redirect with real path/port |
| v1.16 | 3X-UI migrate UI: DB-only upload, Full badge, redirect verify commands |
| v1.15.5 | Fix Errno 36 when PasarGuard PEM in .env was treated as a file path |
| v1.15.4 | Redirect TLS from PasarGuard certs; HTTP when old sub had no TLS |
| v1.15.3 | HTTPS pg-redirect for old 3x-ui links (PG certs / self-signed) |
| v1.15.2 | pg-redirect install without useradd (fallback to nobody) |
| v1.15 | Native pg-redirect service (no GitHub binary download) |
| v1.14 | Redirect: GitHub download mirrors + show real failure cause |
| v1.13 | Remove PasarGuard DB-change option from panel migrate |
| v1.12 | 3x-ui cert upload warning + disable Hiddify/Remnawave (coming soon) |
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
