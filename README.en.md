> ⚠️ **Beta (`v1.0beta`)** — publicly available, but still experimental. Bugs are possible; use with caution.

<p align="center">
  <a href="README.md">فارسی</a> · <b>English</b> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# 🕒 PGClockMG

**Restore and migrate to PasarGuard**

`v1.0beta` · port `7000` · فارسی / English / Русский

</div>

---

## ⚠️ Before you start

| | |
|---|---|
| 🧪 **Beta** | This build is not fully stabilized yet. If you run it on a production server, accept the risk. |
| 💾 **Back up first** | Before restore or migration, take a full backup of the panel, database, and important files. |
| 🛠️ **Panel install** | This wizard does **not** install PasarGuard. Install the panel yourself first, then come back for restore or migration. |

---

## ✨ What does this tool do?

| Task | Description |
|------|-------------|
| ♻️ **Restore / change database** | Restores a PasarGuard backup — even when the database type differs from before |
| 🚚 **Migrate** | Moves data from panels such as Marzban, 3x-ui, Remnawave, and Hiddify |
| 📘 **Guide** | Shows live specs if the panel is installed; otherwise only the official install commands |

Note: restore always targets the database currently installed on the server.

---

## 🚀 Install

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

After install, open: **`http://SERVER_IP:7000`**

📋 Requirements: Ubuntu or Debian · root access · Docker · port `7000`

---

## 📦 Panel support

| Source | Status |
|--------|--------|
| 🟢 Marzban | Full |
| 🟢 PasarGuard (restore) | Full |
| 🟡 3x-ui | Partial |
| 🟠 Remnawave / Hiddify | Experimental |

---

## 🧰 Useful commands

```bash
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# Update to the latest version
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# When you are done
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## 🔒 Privacy

Everything runs only on your own server. Backups and passwords are not sent anywhere outside your server.

---

## 📄 License

**Copyright (c) 2026 Mrclocks — All rights reserved.**

Personal install and use on your own server is allowed.  
Copying, republishing, selling, or use without permission and without attribution is **not allowed** and may be enforced (including via DMCA on GitHub).

Details: [`LICENSE`](LICENSE) · Repo: [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)
