> ⚠️ **`v2.2.5`** — Перед restore или миграцией сделайте полный бэкап.

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
| 3X-UI | Полная |
| Remnawave | Скоро |
| Hiddify | Частично (пользователи + redirect ссылок) |

---

## История версий

| Версия | Основные изменения |
|--------|-------------------|
| v2.2.5 | Исправление heal restore MySQL, когда пароль root в volume не совпадает с .env (все кандидаты, затем безопасный skip-grant recovery на том же volume) |
| v2.2.4 | Исправление NameError миграции Marzban cross-DB: отсутствовал import PASARGUARD_ENV в panel-boot |
| v2.2.3 | Исправление краша миграции modern 3x-ui: пустой uuid + числовой clients.id |
| v2.2.2 | Освобождение порта 443 Hiddify для pg-redirect |
| v2.2.1 | Гайд owner после миграции + ясное предупреждение Hiddify |
| v2.2.0 | Переписывание миграции Hiddify: группа hiddify-test + PYTHONPATH=/code + redirect как 3x-ui |
| v2.1.2 | Исправление краша импорта пользователей Hiddify (авто-группа + полный лог) |
| v2.1.1 | Исправление загрузки JSON Hiddify + без выбора БД/пароля; полностью автоматически |
| v2.1 | Частичная миграция Hiddify: пользователи с тем же UUID, redirect старых ссылок, только JSON |
| v2.0.6 | Увеличено ожидание panel-boot, пока Alembic ещё выполняется (без раннего удаления staging MySQL) |
| v2.0.5 | Исправление пустого JSON при старте миграции + блокировка параллельных запусков |
| v2.0.4 | Исправление миграции Marzban MySQL→Timescale при неизвестном alembic_version (heal только на staging) |
| v2.0.3 | Исправление restore Timescale: детект старого каталога chunk.schema_name и pin образа на 2.28.3 для хостов 2.29+ |
| v2.0.2 | Исправление миграции MariaDB→Timescale (CREATE DATABASE + staging на mariadb:11) |
| v2.0.1 | Redirect/панель: домен PasarGuard приоритетнее IP; отслеживает смену .env/домена |
| v2.0 | Полная миграция 3X-UI (авто old/new, Host/Admin/Group, redirect path/port) |
| v1.16.3 | Исправление Internal Server Error на /sub (короткий пароль shadowsocks из 3x-ui) |
| v1.16.2 | Автоопределение схемы 3x-ui + Hosts/Admin чтобы /sub работал на новых панелях |
| v1.16.1 | Полная миграция современного 3x-ui (multi-inbound) + redirect с реальным path/port |
| v1.16 | UI миграции 3X-UI: только DB, бейдж Full, команды проверки redirect |
| v1.15.5 | Исправление Errno 36: PEM из .env PasarGuard больше не воспринимается как путь |
| v1.15.4 | TLS редиректа из сертификатов PasarGuard; HTTP если у старого sub не было TLS |
| v1.15.3 | HTTPS pg-redirect для старых ссылок 3x-ui (сертификаты PG / self-signed) |
| v1.15.2 | Установка pg-redirect без useradd (fallback на nobody) |
| v1.15 | Нативный pg-redirect (без скачивания с GitHub) |
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
