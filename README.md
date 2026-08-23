<div dir="rtl" align="right">

> ⚠️ **`v3.2.8`** — قبل از ریستور یا مهاجرت حتماً بکاپ کامل بگیرید.

<p align="center">
  <b>فارسی</b> · <a href="README.en.md">English</a> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

ویزارد وب برای ریستور و مهاجرت به PasarGuard

</div>

---

## معرفی

PGClockMG یک ویزارد وب برای **ریستور بکاپ** و **مهاجرت به PasarGuard** است.

### این ویزارد چه کار می‌کند؟

- ریستور بکاپ PasarGuard — حتی با تغییر نوع دیتابیس (SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB)
- رفع خودکار مشکلات نسخه TimescaleDB هنگام ریستور (pull تصویر، readiness probe، auth fallback)
- نمایش اطلاعات دیتابیس بعد از آپلود بکاپ — مقایسه DB بکاپ با DB نصب‌شده
- گزینه غیرفعال‌سازی نودها بعد از ریستور (برای جلوگیری از تداخل با پنل قبلی)
- مهاجرت از Marzban، 3x-ui، Hiddify و Remnawave
- راهنمای وضعیت پنل و دستور نصب رسمی

> این ویزارد خودش PasarGuard را نصب نمی‌کند. اول پنل را نصب کنید، بعد برگردید.

---

## پیش‌نیازها

- Ubuntu 22.04+
- دسترسی `root`
- Docker
- PasarGuard از قبل روی سرور جدید نصب شده باشد

آدرس پنل بعد از نصب: `http://SERVER_IP:PORT/?token=...`  
پورت پیش‌فرض: `7000`

ویزارد با **توکن دسترسی** محافظت می‌شود. بدون توکن کسی به پنل دسترسی ندارد.
نصب‌کننده در پایان یک URL آماده با توکن چاپ می‌کند (`http://SERVER_IP:PORT/?token=...`).
در صورت نیاز برای بازیابی:

```bash
cat /opt/pg-migrator/.access_token
```

---

## نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

بعد از اجرا، اسکریپت صفحه را پاک می‌کند و منوی اصلی را نشان می‌دهد.

### منوی اسکریپت

| گزینه | کار |
|-------|-----|
| ۱ · Install / update | پورت وب‌پنل را می‌پرسد (Enter یعنی همان `7000`)، نصب را کامل می‌کند و در آخر URL ورود (همراه توکن) را می‌دهد |
| ۲ · Uninstall | سرویس و `/opt/pg-migrator` را پاک می‌کند و پیشنهاد می‌دهد بکاپ‌ها را نگه دارید. PasarGuard، دیتابیس‌ها و ریدایرکت سرور دست‌نخورده می‌مانند |
| ۳ · Redirect server | وضعیت، ری‌استارت، ری‌استارت اجباری و لاگ سرویس `pg-redirect` — فقط برای مهاجرت 3x-ui و Hiddify |
| ۴ · Exit | خروج از منو |

بالای منو وضعیت PasarGuard (نصب بودن، نسخه، نوع دیتابیس)، Docker، سرویس ویزارد و
ریدایرکت سرور نمایش داده می‌شود. اگر PasarGuard نصب نباشد، اسکریپت به شما می‌گوید
که اول باید پنل را نصب کنید؛ این ویزارد فقط داده را به پنل موجود منتقل می‌کند.

---

## آموزش استفاده

### مراحل اصلی

۱. از پنل فعلی خود بکاپ بگیرید.  
۲. PasarGuard را با دیتابیس دلخواه خودتان روی **سرور جدید** نصب کنید. نوع دیتابیس
   پنل قبلی مهم نیست؛ اگر متفاوت باشد، اسکریپت تبدیل/مهاجرت لازم را خودکار انجام می‌دهد.  
۳. مطمئن شوید پنل جدید بالا آمده و قابل دسترس است، سپس پنل قبلی را موقتاً غیرفعال کنید.  
۴. اسکریپت PGClockMG را روی سرور جدید نصب کنید.  
۵. در پایان نصب، اسکریپت URL وب‌پنل (همراه توکن) را نشان می‌دهد. همان لینک را باز کنید و مراحل را قدم‌به‌قدم جلو ببرید.  
۶. ریستور یا مهاجرت را انجام دهید.

### ترتیب پیشنهادی

- اول از پنل قبلی بکاپ بگیرید
- بعد PasarGuard را روی سرور جدید نصب و تست کنید
- موقتاً پنل قبلی را غیرفعال کنید تا لینک، پورت یا نود تداخل نداشته باشد
- در نهایت از وب‌پنل PGClockMG ریستور یا مهاجرت را اجرا کنید

---

## نکات مهم

### ورود به وب‌پنل (توکن دسترسی)

ویزارد بدون توکن باز نمی‌شود. بعد از نصب، همان URL چاپ‌شده توسط
نصب‌کننده را باز کنید (`http://SERVER_IP:PORT/?token=...`). اگر لینک را از دست دادید:

```bash
cat /opt/pg-migrator/.access_token
```

توکن هنگام نصب در `/opt/pg-migrator/.access_token` با دسترسی `0600` ساخته می‌شود.
آن را در چت عمومی یا اسکرین‌شات عمومی قرار ندهید.

### بعد از مهاجرت 3x-ui / Hiddify لینک‌های قدیمی کار نمی‌کنند؟

`pg-redirect` باید روی همان پورت پنل قبلی بالا بیاید. اگر پنل قبلی هنوز روشن باشد،
پورت را نگه می‌دارد و سرویس ریدایرکت بالا نمی‌آید. اسکریپت را اجرا کنید، گزینه `3`
(Redirect server) و بعد `2` (Force restart): پنل قبلی را می‌خواباند، پورت‌ها را آزاد
می‌کند و ریدایرکت را با health check دوباره بالا می‌آورد.

### نصب/اجرا بدون سوال

```bash
sudo PG_MIGRATOR_PORT=8443 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
sudo PG_MIGRATOR_ACTION=redirect-restart bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

مقدارهای `PG_MIGRATOR_ACTION`: ‏`install`، `uninstall`، `redirect-restart` یا `menu`  
با `PG_MIGRATOR_YES=1` همه تاییدها خودکار «بله» می‌شوند.

---

## پشتیبانی پنل‌ها

| پنل | وضعیت |
|-----|--------|
| Marzban | کامل |
| PasarGuard | ریستور / تغییر DB (نه در مهاجرت پنل) |
| 3X-UI | کامل |
| Hiddify | ناقص (کاربران + ریدایرکت لینک) |
| Remnawave | به‌زودی |

---

## دستورات کاربردی

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
