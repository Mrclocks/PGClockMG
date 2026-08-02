<div dir="rtl" align="right">

> ⚠️ **`v1.13`** — قبل از ریستور یا مهاجرت حتماً بکاپ کامل بگیرید.

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
- مهاجرت از Marzban، 3x-ui، Remnawave و Hiddify
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
| 3x-ui | جزئی |
| Remnawave / Hiddify | به‌زودی |

---

## تاریخچه نسخه‌ها

| نسخه | تغییرات اصلی |
|------|--------------|
| v1.13 | حذف گزینه تغییر DB PasarGuard از بخش مهاجرت پنل |
| v1.12 | هشدار سرتیفیکیت 3x-ui + غیرفعال‌سازی موقت Hiddify/Remnawave |
| v1.11 | نصب مستقیم redirect-server (بدون اسکریپت شکنندهٔ رسمی) |
| v1.10 | لینک‌های قدیمی 3x-ui: نرمال‌سازی mapping + پورت/دامنه redirect واقعی |
| v1.9 | رفع Access denied بعد مهاجرت 3x-ui→MySQL + نصب صحیح redirect سابسکریپشن |
| v1.8 | رفع مهاجرت 3x-ui به MySQL (پروب DB + core_configs) |
| v1.7 | رفع خطای مهاجرت 3x-ui هنگام آپلود bundle (IsADirectoryError) |
| v1.6beta | بهبود UI کارت DB، سوییچ iOS برای نودها، لوگو جدید |
| v1.5beta | گزینه غیرفعال‌سازی نودها، نمایش مشخصات DB بعد آپلود |
| v1.4beta | رفع restart loop ساکت پنل بعد ریستور TimescaleDB |
| v1.3beta | pull تصویر Docker، readiness probe، auth fallback بعد wipe |
| v1.2beta | رفع خطای «role already exists» هنگام ریستور globals.sql |
| v1.1beta | انتشار اولیه |

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
