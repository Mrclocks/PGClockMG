# PG-Migrator

سیستم جامع مهاجرت از پنل‌های مختلف به **PasarGuard** با رابط وب گرافیکی.

## نصب با یک دستور (Ubuntu)

```bash
# از مسیر پروژه:
sudo bash install.sh

# یا با curl (بعد از آپلود به GitHub):
curl -fsSL https://raw.githubusercontent.com/YOUR_USER/pg-migrator/main/install.sh | sudo bash
```

بعد از نصب، مرورگر را باز کنید:

```
http://SERVER_IP:7000
```

## پنل‌های پشتیبانی‌شده

| پنل مبدأ | سطح پشتیبانی | لینک اشتراک | دیتابیس مبدأ |
|----------|-------------|-------------|--------------|
| **Marzban** | کامل | حفظ می‌شود | SQLite, MySQL, MariaDB |
| **3x-ui** | جزئی | تغییر می‌کند* | SQLite |
| **Hiddify** | آزمایشی | تغییر می‌کند | MySQL, MariaDB |
| **PasarGuard** | تغییر DB | حفظ می‌شود | همه |

\* برای 3x-ui می‌توانید سرور ریدایرکت لینک‌های قدیمی را نصب کنید.

## پیش‌نیازها

- Ubuntu 20.04+ (یا Debian)
- دسترسی root
- Docker و Docker Compose
- برای مهاجرت Marzban درجا: Marzban نصب روی سرور
- برای 3x-ui و Hiddify: PasarGuard باید نصب باشد

## مراحل ویزارد

1. **پیش‌نیازها** — بررسی وضعیت سرور
2. **پنل مبدأ** — انتخاب Marzban / 3x-ui / Hiddify / PasarGuard
3. **دیتابیس مبدأ** — نوع DB + آپلود بکاپ (zip/sql/sqlite)
4. **دیتابیس مقصد** — پیشنهاد هوشمند بر اساس مبدأ
5. **تأیید** — خلاصه + گزینه ریدایرکت
6. **مهاجرت** — لاگ زنده
7. **نتیجه** — لینک پنل PasarGuard

## آپلود بکاپ

فایل‌های پشتیبانی‌شده:
- `.zip` — بکاپ کامل (استخراج خودکار)
- `.sql` — dump MySQL/MariaDB
- `.sqlite3` / `.db` — دیتابیس SQLite (Marzban, x-ui)

## دستورات مفید

```bash
# وضعیت سرویس
systemctl status pg-migrator

# ری‌استارت وب‌پنل
systemctl restart pg-migrator

# مشاهده لاگ
journalctl -u pg-migrator -f
tail -f /opt/pg-migrator/logs/service.log
```

## ساختار پروژه

```
pg-migrator/
├── install.sh          # نصب یک‌خطی
├── requirements.txt
├── app/
│   ├── main.py         # FastAPI backend
│   ├── panels.py       # تعریف پنل‌ها و قابلیت‌ها
│   ├── config.py
│   ├── models.py
│   ├── services/
│   │   ├── prerequisites.py
│   │   ├── orchestrator.py
│   │   ├── upload.py
│   │   └── migrators/
│   │       ├── marzban.py    # مهاجرت رسمی Marzban
│   │       ├── xui.py        # ابزار رسمی PasarGuard/migrations
│   │       ├── hiddify.py    # آزمایشی
│   │       └── pasarguard_db.py  # ابزار رسمی db-migrations
│   └── static/         # رابط وب تیره
└── tools/              # کلون خودکار ابزارهای PasarGuard
```

## منابع رسمی

- [Marzban → PasarGuard](https://docs.pasarguard.org/en/migration/marzban/)
- [PasarGuard db-migrations](https://github.com/PasarGuard/db-migrations)
- [PasarGuard panel migrations](https://github.com/PasarGuard/migrations)
- [PasarGuard Panel](https://github.com/PasarGuard/panel)

## نکات امنیتی

- همیشاً قبل از مهاجرت بکاپ کامل بگیرید
- وب‌پنل فقط روی پورت 7000 بدون SSL اجرا می‌شود — در پروداکشن با فایروال محدود کنید
- بعد از مهاجرت موفق، سرویس `pg-migrator` را غیرفعال کنید:

```bash
systemctl stop pg-migrator
systemctl disable pg-migrator
```

## لایسنس

MIT — استفاده آزاد با مسئولیت خودتان.
