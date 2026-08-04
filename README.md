<div dir="rtl" align="right">

> ⚠️ **`v2.0.5`** — قبل از ریستور یا مهاجرت حتماً بکاپ کامل بگیرید.

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
| 3X-UI | کامل |
| Remnawave / Hiddify | به‌زودی |

---

## تاریخچه نسخه‌ها

| نسخه | تغییرات اصلی |
|------|--------------|
| v2.0.5 | فیکس خطای JSON خالی هنگام شروع مهاجرت + جلوگیری از اجرای هم‌زمان (پورت staging) |
| v2.0.4 | فیکس مهاجرت Marzban MySQL→Timescale وقتی alembic_version مرزبان در PasarGuard نیست (heal فقط روی staging) |
| v2.0.3 | فیکس ریستور Timescale: تشخیص کاتالوگ قدیمی chunk.schema_name و پین ایمیج به 2.28.3 روی سرورهای 2.29+ |
| v2.0.2 | فیکس مهاجرت MariaDB→Timescale (ساخت DB موقت + staging با mariadb:11) |
| v2.0.1 | ریدایرکت/لینک پنل: اولویت دامنه پاسارگارد، در غیر این‌صورت IP؛ دنبال‌کردن تغییر دامنه/.env |
| v2.0 | مهاجرت کامل 3X-UI قدیم/جدید (تشخیص خودکار، Host/Admin/Group، ریدایرکت path/port) |
| v1.16.3 | فیکس Internal Server Error روی /sub (پسورد shadowsocks کوتاه از 3x-ui) |
| v1.16.2 | تشخیص خودکار اسکیما 3x-ui + ساخت Host/Admin و فیکس /sub برای نسخه جدید |
| v1.16.1 | مهاجرت کامل 3x-ui مدرن (multi-inbound) + ریدایرکت با path/port واقعی |
| v1.16 | UI مهاجرت 3X-UI: آپلود فقط DB، تگ کامل، دستورات چک ریدایرکت |
| v1.15.5 | فیکس Errno 36 وقتی PEM پاسارگارد به‌اشتباه به‌عنوان مسیر فایل خوانده می‌شد |
| v1.15.4 | سرت ریدایرکت از پاسارگارد؛ HTTP اگر ساب قدیمی TLS نداشت |
| v1.15.3 | ریدایرکت HTTPS برای لینک‌های قدیمی 3x-ui (سرت PG / self-signed) |
| v1.15.2 | نصب pg-redirect بدون useradd (fallback به nobody) |
| v1.15 | ریدایرکت بومی pg-redirect (بدون دانلود GitHub) |
| v1.14 | ریدایرکت: آینه دانلود GitHub + نمایش علت واقعی در هشدار |
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
