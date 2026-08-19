> ⚠️ **`v3.2.3`** — Перед restore или миграцией сделайте полный бэкап.

<p align="center">
  <a href="README.md">فارسی</a> · <a href="README.en.md">English</a> · <b>Русский</b>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

Веб-мастер для восстановления и миграции на PasarGuard

</div>

---

## Обзор

PGClockMG — это веб-мастер для **восстановления бэкапов** и **миграции в PasarGuard**.

### Что умеет мастер

- Восстановление бэкапов PasarGuard — включая смену СУБД (SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB)
- Автоматическое устранение несоответствия версий TimescaleDB при restore (pull образа, readiness probe, auth fallback)
- Карточка с информацией о БД после загрузки бэкапа — сравнивает СУБД бэкапа и установленную
- Опция отключения узлов после восстановления, чтобы избежать конфликтов с ещё активной старой панелью
- Миграция с Marzban, 3x-ui, Hiddify и Remnawave
- Показ статуса панели и официальных шагов установки

> Мастер не устанавливает PasarGuard за вас. Сначала установите панель, затем вернитесь.

---

## Требования

- Ubuntu 22.04+
- доступ `root`
- Docker
- PasarGuard уже установлен на новом сервере

Адрес панели после установки: `http://SERVER_IP:PORT`  
Порт по умолчанию: `7000`

---

## Установка

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

После запуска скрипт очищает экран и показывает главное меню.

### Меню скрипта

| Пункт | Что делает |
|-------|------------|
| 1 · Install / update | Спрашивает порт веб-панели (Enter — `7000`), завершает установку и в конце печатает адрес входа |
| 2 · Uninstall | Удаляет сервис и `/opt/pg-migrator`, предлагая сохранить бэкапы. PasarGuard, базы и redirect-сервер не трогает |
| 3 · Redirect server | Статус, перезапуск, принудительный перезапуск и логи `pg-redirect` — только для миграций 3x-ui / Hiddify |
| 4 · Exit | Выход из меню |

Над меню видно состояние PasarGuard (установлен, версия, движок БД), Docker,
сервиса мастера и redirect-сервера. Если PasarGuard не найден, скрипт сообщит об
этом — он переносит данные только в уже существующую панель.

---

## Как пользоваться

### Основной порядок

1. Сделайте бэкап текущей панели.  
2. Установите PasarGuard с нужной вам базой данных на **новом сервере**. Тип БД
   старой панели не важен; если он отличается, мастер выполнит нужную
   конвертацию/миграцию автоматически.  
3. Убедитесь, что новая панель запущена и доступна, затем временно отключите старую панель.  
4. Установите скрипт PGClockMG на новый сервер.  
5. В конце установки скрипт покажет адрес веб-панели. Откройте его и пройдите шаги мастера.  

### Рекомендуемая последовательность

- Сначала сделайте бэкап старой панели
- Затем установите и проверьте PasarGuard на новом сервере
- Временно отключите старую панель, чтобы не было конфликтов ссылок, портов или нод
- После этого выполните restore или миграцию через веб-панель PGClockMG

---

## Важные заметки

### После миграции 3x-ui / Hiddify не работают старые ссылки?

`pg-redirect` должен занять порт старой панели. Если она всё ещё запущена, порт
занят и сервис не стартует. Запустите скрипт, выберите `3` (Redirect server), затем
`2` (Force restart): старая панель останавливается, порты освобождаются, redirect
поднимается снова с проверкой здоровья.

### Режим без вопросов

```bash
sudo PG_MIGRATOR_PORT=8443 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
sudo PG_MIGRATOR_ACTION=redirect-restart bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

`PG_MIGRATOR_ACTION`: `install`, `uninstall`, `redirect-restart` или `menu`  
`PG_MIGRATOR_YES=1` подтверждает все вопросы автоматически.

---

## Поддержка панелей

| Панель | Статус |
|--------|--------|
| Marzban | Полная |
| PasarGuard | Restore / смена БД (не в миграции панелей) |
| 3X-UI | Полная |
| Hiddify | Частично (пользователи + redirect ссылок) |
| Remnawave | Скоро |

---

## Полезные команды

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
