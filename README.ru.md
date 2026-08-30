> 🚀 **`v4.0.7`** — Полировка вёрстки панели бэкапа (кнопки, стрим, дашборд)
> ⚠️ Перед restore или миграцией сделайте полный бэкап.

<p align="center">
  <a href="README.md">فارسی</a> · <a href="README.en.md">English</a> · <b>Русский</b>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

🧰 Мастер restore/миграции + панель бэкапа PasarGuard (раздельная установка)

</div>

---

## ✨ Обзор

| Продукт | Путь | Порт | Сервис |
|---------|------|------|--------|
| 🧭 **PGClockMG** (мастер) | `/opt/pg-migrator` | `7000` | `pg-migrator` |
| 💾 **PGClockBackup** | `/opt/pg-backup` | `7001` | `pg-backup` |

С **v4.0.1** они ставятся и удаляются **независимо**. Удаление мастера после restore **не** трогает панель бэкапа.

> 📌 PasarGuard устанавливайте сами.

---

## 📥 Установка

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

### 🗂️ Меню

| Пункт | Действие |
|-------|----------|
| 1 · Install PGClockMG | Только мастер |
| 2 · Install PGClockBackup | Только бэкап + **одноразовый setup-токен** |
| 3 · Uninstall PGClockMG | Только мастер (бэкап остаётся) |
| 4 · Uninstall PGClockBackup | Только панель бэкапа |
| 5 · Redirect server | 3x-ui / Hiddify |
| 6 · Exit | Выход |

```bash
sudo PG_MIGRATOR_ACTION=install-wizard PG_MIGRATOR_PORT=7000 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
sudo PG_MIGRATOR_ACTION=install-backup PG_BACKUP_PORT=7001 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

---

## 🧭 Мастер (PGClockMG)

`http://SERVER_IP:7000/?token=...`

После успешного restore можно удалить мастер — PGClockBackup останется.

```bash
cat /opt/pg-migrator/.access_token
```

---

## 💾 Панель бэкапа (PGClockBackup)

`http://SERVER_IP:7001/` · первый вход: **setup-токен** + сильный пароль

Полный бандл, расписание, Telegram, stream → ручное подтверждение restore. Отдельный путь `/opt/pg-backup`.

```bash
cat /opt/pg-backup/backup_panel/.setup_token
```

---

## 🛠️ Команды

```bash
systemctl status pg-migrator
systemctl status pg-backup
```

Точка отката до v4: `restore-point-pre-v4.0.0`

---

## 📄 Лицензия

**Copyright (c) 2026 Mrclocks — Все права защищены.**

[`LICENSE`](LICENSE) · [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)
