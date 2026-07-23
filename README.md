> ⚠️ نسخه آزمایشی — احتمال باگ وجود دارد.

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

**ریستور و مهاجرت به PasarGuard**  
ویزارد وب روی سرور خودتان · `v2.8.10` · پورت `7000` · FA / EN / RU

</div>

---

## چه کاری می‌کند؟

| هدف | توضیح |
|-----|--------|
| **ریستور / تغییر DB** | بکاپ PasarGuard را برمی‌گرداند — حتی اگر نوع دیتابیس فرق کند |
| **مهاجرت** | انتقال از Marzban، 3x-ui، Remnawave، Hiddify و … |
| **راهنما** | اگر پنل نصب است مشخصاتش را نشان می‌دهد؛ اگر نه، فقط دستور نصب رسمی |

> ویزارد **خودش PasarGuard نصب نمی‌کند**. اول خودتان نصب کنید، بعد ریستور یا مهاجرت.

مقصد ریستور همیشه دیتابیس **نصب‌شده** روی سرور است.

---

## نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

باز کنید: **`http://SERVER_IP:7000`**

Ubuntu/Debian · root · Docker · پورت ۷۰۰۰

---

## پنل‌ها

| مبدأ | وضعیت |
|------|--------|
| Marzban | کامل |
| PasarGuard (ریستور) | کامل |
| 3x-ui | جزئی |
| Remnawave / Hiddify | آزمایشی |

---

## دستورات

```bash
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# آپدیت
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# بعد از اتمام کار
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## حریم خصوصی

همه‌چیز فقط روی سرور شماست. بکاپ و رمزها جایی نمی‌روند.

---

## مجوز

این پروژه تحت **[GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE)** منتشر می‌شود — همان لایسنس پنل [PasarGuard](https://github.com/PasarGuard/panel).

Copyright (c) 2026 Mrclocks
