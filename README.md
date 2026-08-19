<div dir="rtl" align="right">

> ⚠️ **`v3.2.2`** — قبل از ریستور یا مهاجرت حتماً بکاپ کامل بگیرید.

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

اسکریپت صفحه را پاک می‌کند و یک منو نشان می‌دهد:

| گزینه | کار |
|-------|-----|
| ۱ · Install / update | پورت وب‌پنل را می‌پرسد (Enter یعنی همان `7000`)، نصب را کامل انجام می‌دهد و در آخر آدرس ورود را می‌دهد |
| ۲ · Uninstall | سرویس و `/opt/pg-migrator` را پاک می‌کند و پیشنهاد می‌دهد بکاپ‌ها را نگه دارد. پاسارگارد، دیتابیس‌ها و ریدایرکت سرور دست‌نخورده می‌مانند |
| ۳ · Redirect server | وضعیت، ری‌استارت، ری‌استارت اجباری و لاگ سرویس `pg-redirect` — فقط برای مهاجرت 3x-ui و Hiddify |
| ۴ · Exit | خروج از منو |

بالای منو وضعیت پاسارگارد (نصب بودن، نسخه، نوع دیتابیس)، داکر، سرویس ویزارد و
ریدایرکت سرور نمایش داده می‌شود. اگر پاسارگارد نصب نباشد پیام می‌دهد که اول باید
پنل نصب شود؛ این ویزارد فقط داده را به پنل موجود منتقل می‌کند.

آدرس: `http://SERVER_IP:PORT` (پیش‌فرض `http://SERVER_IP:7000`)  
پیش‌نیاز: Ubuntu/Debian · root · Docker · پاسارگارد از قبل نصب‌شده

## آموزش استفاده

۱. از پنل فعلی خود بکاپ بگیرید.  
۲. PasarGuard را با دیتابیس دلخواه خودتان روی **سرور جدید** نصب کنید. نوع دیتابیس
   پنل قبلی مهم نیست؛ اگر متفاوت باشد، اسکریپت تبدیل/مهاجرت لازم را خودکار انجام می‌دهد.  
۳. مطمئن شوید پنل جدید بالا آمده و قابل دسترس است، سپس پنل قبلی را موقتاً غیرفعال کنید.  
۴. اسکریپت PGClockMG را روی سرور جدید نصب کنید.  
۵. در پایان نصب، اسکریپت آدرس وب‌پنل را نشان می‌دهد. وارد وب‌پنل شوید و مراحل را قدم‌به‌قدم جلو ببرید.  

### منوی اسکریپت چه می‌کند؟

| گزینه | کار |
|-------|-----|
| ۱ · Install / update | ویزارد وب را نصب/به‌روزرسانی می‌کند و در انتها آدرس ورود را می‌دهد |
| ۲ · Uninstall | سرویس و فایل‌های ویزارد را حذف می‌کند |
| ۳ · Redirect server | برای مهاجرت‌های 3x-ui و Hiddify وضعیت و مدیریت `pg-redirect` را نشان می‌دهد |
| ۴ · Exit | خروج از منو |

### ترتیب پیشنهادی کار

- اول از پنل قبلی بکاپ بگیرید  
- بعد PasarGuard را روی سرور جدید نصب و تست کنید  
- سپس پنل قبلی را موقتاً خاموش کنید تا تداخلی با لینک‌ها، پورت‌ها یا نودها نداشته باشد  
- در آخر، از داخل وب‌پنل PGClockMG ریستور یا مهاجرت را انجام دهید  

### بعد از مهاجرت 3x-ui / Hiddify لینک‌های قدیمی کار نمی‌کنند؟

`pg-redirect` باید روی همان پورت پنل قبلی بالا بیاید. اگر پنل قبلی هنوز روشن باشد
پورت را نگه می‌دارد و سرویس ریدایرکت بالا نمی‌آید. اسکریپت را اجرا کنید، گزینه `3`
(Redirect server) و بعد `2` (Force restart): پنل قبلی را می‌خواباند، پورت‌ها را آزاد
می‌کند و ریدایرکت را با health check دوباره بالا می‌آورد.

### بدون سوال

```bash
sudo PG_MIGRATOR_PORT=8443 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
sudo PG_MIGRATOR_ACTION=redirect-restart bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

مقدارهای `PG_MIGRATOR_ACTION`: ‏`install`، `uninstall`، `redirect-restart` یا `menu`.
با `PG_MIGRATOR_YES=1` همه تاییدها خودکار «بله» می‌شوند.

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
