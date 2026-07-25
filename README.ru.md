> ⚠️ **Бета `v1.1beta`** — экспериментальная версия. Перед restore или миграцией сделайте полный бэкап.

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

- Восстановление бэкапов PasarGuard (в том числе со сменой СУБД)
- Миграция с Marzban, 3x-ui, Remnawave и Hiddify
- Статус панели и официальная установка

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
| PasarGuard | Полная |
| 3x-ui | Частичная |
| Remnawave / Hiddify | Экспериментальная |

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
