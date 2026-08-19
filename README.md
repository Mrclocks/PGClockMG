<div dir="rtl" align="right">

> ⚠️ **`v3.1.5`** — قبل از ریستور یا مهاجرت حتماً بکاپ کامل بگیرید.

<p align="center">
  <b>فارسی</b> · <a href="README.en.md">English</a> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

ویزارد وب برای ریستور و مهاجرت به PasarGuard

</div>

---

## امکانات

- ریستور بکاپ PasarGuard — حتی با تغییر نوع دیتابیس (SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB)
- رفع خودکار مشکلات نسخه TimescaleDB هنگام ریستور (pull تصویر، readiness probe، auth fallback)
- نمایش اطلاعات دیتابیس بعد از آپلود بکاپ — مقایسه DB بکاپ با DB نصب‌شده
- گزینه غیرفعال‌سازی نودها بعد از ریستور (برای جلوگیری از تداخل با پنل قبلی)
- مهاجرت از Marzban، 3x-ui، Hiddify و Remnawave
- راهنمای وضعیت پنل و دستور نصب رسمی

> این ویزارد خودش PasarGuard را نصب نمی‌کند. اول پنل را نصب کنید، بعد برگردید.

---

## نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

آدرس: `http://SERVER_IP:7000`  
پیش‌نیاز: Ubuntu/Debian · root · Docker

---

## پشتیبانی

| پنل | وضعیت |
|-----|--------|
| Marzban | کامل |
| PasarGuard | ریستور / تغییر DB (نه در مهاجرت پنل) |
| 3X-UI | کامل |
| Hiddify | ناقص (کاربران + ریدایرکت لینک) |
| Remnawave | به‌زودی |

---

## دستورات

```bash
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# به‌روزرسانی
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# توقف
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## مجوز

**Copyright (c) 2026 Mrclocks — همه حقوق محفوظ است.**

استفاده شخصی روی سرور خودتان آزاد است.  
کپی، بازنشر یا فروش بدون اجازه مجاز نیست.

[`LICENSE`](LICENSE) · [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)

</div>
