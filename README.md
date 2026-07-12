# PG-Migrator

**نسخه 1.1.0** — سیستم مهاجرت از پنل‌های مختلف به [PasarGuard](https://github.com/PasarGuard/panel) با ویزارد وب گرافیکی.

**Languages:** Web UI — English · فارسی · Русский | Installer script — English only

**Repository:** [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)

---

## نصب سریع (Ubuntu)

```bash
# روش پیشنهادی
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh)"

# اگر نسخه قدیمی cache شده
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# یا دانلود و اجرای مستقیم
curl -fsSL "https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh" -o /tmp/pg-install.sh
grep SCRIPT_VERSION /tmp/pg-install.sh   # باید 1.1.0 باشد
sudo bash /tmp/pg-install.sh
```

بعد از نصب:

```
http://SERVER_IP:7000
```

زبان رابط را از هدر (EN / FA / RU) انتخاب کنید.

---

## پنل‌های پشتیبانی‌شده

| پنل مبدأ | سطح | لینک اشتراک | دیتابیس مبدأ |
|----------|-----|-------------|--------------|
| **Marzban** | کامل | حفظ می‌شود | SQLite, MySQL, MariaDB |
| **3x-ui** | جزئی | با redirect server حفظ می‌شود* | SQLite |
| **Remnawave** | آزمایشی | تغییر می‌کند | PostgreSQL (API) |
| **Hiddify** | آزمایشی | تغییر می‌کند | MySQL, MariaDB |
| **PasarGuard** | تغییر DB | حفظ می‌شود | همه |

\* طبق [ابزار رسمی x-ui](https://github.com/PasarGuard/migrations/tree/main/x-ui): توکن PasarGuard فرمت جدید دارد، ولی با **redirect server** (پیش‌فرض فعال در ویزارد) لینک‌های قدیمی `/sub/{token}` برای کاربران کار می‌کنند.

---

## چه چیزهایی باید **قبل از مهاجرت** نصب باشد؟

| پنل مبدأ | PasarGuard قبل از مهاجرت؟ | پنل مبدأ / داده |
|----------|---------------------------|-----------------|
| **Marzban** | خیر — خودکار نصب می‌شود | Marzban روی همین سرور **یا** آپلود بکاپ |
| **3x-ui** | **بله — حتماً قبل** | فایل `x-ui.db` یا آپلود بکاپ |
| **Remnawave** | **بله — حتماً قبل** | URL پنل + API Token (می‌تواند روی سرور دیگر باشد) |
| **Hiddify** | **بله — حتماً قبل** | dump MySQL یا دیتابیس زنده روی سرور |
| **PasarGuard (DB)** | **بله — در حال اجرا** | فقط تغییر نوع دیتابیس |

> این اطلاعات در **مرحله ۰ و ۱** ویزارد وب هم نمایش داده می‌شود.

### Marzban
- مهاجرت درجا: Marzban نصب باشد، PasarGuard **نباید** از قبل نصب باشد
- اگر هر دو نصب هستند → از آپلود بکاپ استفاده کنید
- مستند رسمی: [Marzban → PasarGuard](https://docs.pasarguard.org/en/migration/marzban/)

### 3x-ui
1. ابتدا PasarGuard را نصب کنید (خالی و تازه)
2. `x-ui.db` را بدهید یا آپلود کنید
3. redirect server را فعال نگه دارید (پیش‌فرض)
4. ادمین را دستی بسازید: `pasarguard cli generate-temp-key`

### Remnawave (آزمایشی)
- ابزار رسمی PasarGuard برای Remnawave وجود ندارد
- مهاجرت از طریق API — کاربران منتقل می‌شوند
- نودها، squad و inbound باید دستی تنظیم شوند

---

## مراحل ویزارد وب

1. **پیش‌نیازها** — root، Docker، بکاپ
2. **پنل مبدأ** — انتخاب + نمایش دقیق «چه چیز نصب باشد»
3. **دیتابیس مبدأ** — نوع DB، رمز، آپلود بکاپ، فیلدهای Remnawave
4. **دیتابیس مقصد** — پیشنهاد هوشمند + نصب PasarGuard از ویزارد
5. **تأیید** — خلاصه + گزینه redirect (3x-ui)
6. **مهاجرت** — لاگ زنده
7. **نتیجه** — لینک `https://IP:8000/dashboard/`

---

## آپلود بکاپ

| فرمت | کاربرد |
|------|--------|
| `.zip` | بکاپ کامل — استخراج خودکار |
| `.sql` | dump MySQL/MariaDB (Marzban, Hiddify) |
| `.sqlite3` / `.db` | Marzban (`db.sqlite3`) یا 3x-ui (`x-ui.db`) |

حداکثر حجم: ۵۰۰ مگابایت

---

## پیش‌نیازهای سرور

- Ubuntu 20.04+ یا Debian
- دسترسی **root**
- Docker (اگر از قبل نصب است، installer دوباره نصب نمی‌کند — تداخل `containerd` رفع شده)
- پورت **7000** برای ویزارد وب

---

## دستورات مفید

```bash
# وضعیت
systemctl status pg-migrator

# آپدیت بعد از push جدید
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# ری‌استارت فقط وب‌پنل
systemctl restart pg-migrator

# لاگ
journalctl -u pg-migrator -f
tail -f /opt/pg-migrator/logs/service.log
```

---

## ساختار پروژه

```
PGClockMG/
├── install.sh              # نصب یک‌خطی (انگلیسی)
├── requirements.txt
├── app/
│   ├── main.py             # FastAPI backend
│   ├── panels.py           # ماتریس پنل‌ها + پیش‌نیازها (EN/FA/RU)
│   ├── models.py
│   ├── config.py
│   ├── services/
│   │   ├── prerequisites.py
│   │   ├── orchestrator.py
│   │   ├── upload.py
│   │   └── migrators/
│   │       ├── marzban.py       # مستند رسمی Marzban
│   │       ├── xui.py           # PasarGuard/migrations x-ui
│   │       ├── remnawave.py     # آزمایشی — API
│   │       ├── hiddify.py       # آزمایشی
│   │       └── pasarguard_db.py # PasarGuard/db-migrations
│   └── static/
│       ├── index.html
│       ├── css/style.css
│       └── js/
│           ├── i18n.js          # EN / FA / RU
│           └── app.js
└── tools/                  # کلون خودکار ابزارهای PasarGuard
    ├── db-migrations/
    └── migrations/
```

---

## منابع رسمی

- [Marzban → PasarGuard](https://docs.pasarguard.org/en/migration/marzban/)
- [3x-ui migration + redirect](https://github.com/PasarGuard/migrations/tree/main/x-ui)
- [PasarGuard db-migrations](https://github.com/PasarGuard/db-migrations)
- [PasarGuard Panel](https://github.com/PasarGuard/panel)

---

## امنیت

- همیشاً قبل از مهاجرت **بکاپ کامل** بگیرید
- ویزارد روی پورت 7000 **بدون SSL** است — در پروداکشن با فایروال محدود کنید
- بعد از مهاجرت موفق، سرویس را غیرفعال کنید:

```bash
systemctl stop pg-migrator
systemctl disable pg-migrator
```

---

## Changelog

### v1.1.0
- Web UI: English, Persian, Russian
- Installer script: English only
- Remnawave experimental API migration
- Clear per-panel install prerequisites in wizard
- 3x-ui: redirect server enabled by default (old links preserved)
- Docker install conflict fix (`containerd.io` vs `docker.io`)
- `curl | bash` install fix

### v1.0.x
- Initial release: Marzban, 3x-ui, Hiddify, PasarGuard DB migration

---

## License

MIT — use at your own risk.
