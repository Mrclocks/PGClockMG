> ⚠️ **`v1.14`** — Перед restore или миграцией сделайте полный бэкап.

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
- Миграция с Marzban, 3x-ui, Remnawave и Hiddify
- Статус панели и официальная команда установки

> Мастер не устанавливает PasarGuard. Сначала установите панель сами.

---

## Установка

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

Адрес: `http://SERVER_IP:7000`  
Требования: Ubuntu/Debian · root · Docker

---

## Поддержка

| Панель | Статус |
|--------|--------|
| Marzban | Полная |
| PasarGuard | Restore / смена БД (не в миграции панелей) |
| 3x-ui | Частичная |
| Remnawave / Hiddify | Скоро |

---

## История версий

| Версия | Основные изменения |
|--------|-------------------|
| v1.14 | Redirect: зеркала GitHub + реальная причина ошибки в предупреждении |
| v1.13 | Убрана смена БД PasarGuard из раздела миграции панелей |
| v1.12 | Предупреждение о сертификатах 3x-ui + временно отключены Hiddify/Remnawave |
| v1.11 | Прямая установка redirect-server (без хрупкого официального installer) |
| v1.10 | Старые ссылки 3x-ui: нормализация mapping + реальный порт/домен redirect |
| v1.9 | Исправление Access denied после 3x-ui→MySQL + корректный install redirect |
| v1.8 | Исправление миграции 3x-ui → MySQL (probe БД + core_configs) |
| v1.7 | Исправление ошибки миграции 3x-ui при загрузке bundle (IsADirectoryError) |
| v1.6beta | Новый дизайн карточки БД, iOS-переключатель для узлов, новый логотип |
| v1.5beta | Опция отключения узлов после восстановления, карточка БД при загрузке |
| v1.4beta | Исправление тихой петли перезапуска панели после восстановления TimescaleDB |
| v1.3beta | Pull Docker-образа, readiness probe, auth fallback после wipe |
| v1.2beta | Исправление ошибки «role already exists» при восстановлении globals.sql |
| v1.1beta | Первый публичный релиз |

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
