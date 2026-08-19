> ⚠️ **`v3.1.9`** — Перед restore или миграцией сделайте полный бэкап.

<p align="center">
  <a href="README.md">فارسی</a> · <a href="README.en.md">English</a> · <b>Русский</b>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

Веб-мастер для восстановления и миграции на PasarGuard

</div>

---

## Возможности

- Восстановление бэкапов PasarGuard — включая смену СУБД (SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB)
- Автоматическое устранение несоответствия версий TimescaleDB (pull образа, readiness probe, auth fallback)
- Карточка с информацией о БД после загрузки бэкапа — сравнивает СУБД бэкапа и установленную
- Опция отключения узлов после восстановления (избегает конфликтов с ещё активной предыдущей панелью)
- Миграция с Marzban, 3x-ui, Hiddify и Remnawave
- Статус панели и официальная команда установки

> Мастер не устанавливает PasarGuard. Сначала установите панель сами.

---

## Установка

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

Установщик спрашивает порт веб-панели — Enter оставит порт по умолчанию `7000`.
При повторном запуске сохраняется порт текущей установки.

Адрес: `http://SERVER_IP:PORT` (по умолчанию `http://SERVER_IP:7000`)  
Требования: Ubuntu/Debian · root · Docker

Установка без вопросов, на заданном порту:

```bash
sudo PG_MIGRATOR_PORT=8443 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

---

## Поддержка

| Панель | Статус |
|--------|--------|
| Marzban | Полная |
| PasarGuard | Restore / смена БД (не в миграции панелей) |
| 3X-UI | Полная |
| Hiddify | Частично (пользователи + redirect ссылок) |
| Remnawave | Скоро |

---

## Команды

```bash
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# Обновление
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# Остановка
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## Лицензия

**Copyright (c) 2026 Mrclocks — Все права защищены.**

Личное использование на своём сервере разрешено.  
Копирование, перепубликация или продажа без разрешения запрещены.

[`LICENSE`](LICENSE) · [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)
