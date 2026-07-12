# PG-Migrator

**نسخه 1.6.3** — سیستم مهاجرت از پنل‌های مختلف به [PasarGuard](https://github.com/PasarGuard/panel) با ویزارد وب گرافیکی.

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
grep SCRIPT_VERSION /tmp/pg-install.sh   # باید 1.6.3 باشد
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
| **Marzban** | کامل | حفظ می‌شود | SQLite, MySQL, MariaDB — PasarGuard باید از قبل نصب باشد |
| **3x-ui** | جزئی | با redirect server حفظ می‌شود* | SQLite |
| **Remnawave** | آزمایشی | تغییر می‌کند | PostgreSQL (API) |
| **Hiddify** | آزمایشی | تغییر می‌کند | MySQL, MariaDB |
| **PasarGuard** | تغییر DB | حفظ می‌شود | همه |

\* طبق [ابزار رسمی x-ui](https://github.com/PasarGuard/migrations/tree/main/x-ui): توکن PasarGuard فرمت جدید دارد، ولی با **redirect server** (پیش‌فرض فعال در ویزارد) لینک‌های قدیمی `/sub/{token}` برای کاربران کار می‌کنند.

---

## چه چیزهایی باید **قبل از مهاجرت** نصب باشد؟

| پنل مبدأ | PasarGuard قبل از مهاجرت؟ | پنل مبدأ / داده |
|----------|---------------------------|-----------------|
| **Marzban** | **بله — حتماً قبل** | آپلود بکاپ Marzban — نوع DB مبدأ خودکار تشخیص داده می‌شود |
| **3x-ui** | **بله — حتماً قبل** | فایل `x-ui.db` یا آپلود بکاپ |
| **Remnawave** | **بله — حتماً قبل** | URL پنل + API Token (می‌تواند روی سرور دیگر باشد) |
| **Hiddify** | **بله — حتماً قبل** | dump MySQL یا دیتابیس زنده روی سرور |
| **PasarGuard (DB)** | **بله — در حال اجرا** | فقط تغییر نوع دیتابیس |

> این اطلاعات در **مرحله ۰ و ۱** ویزارد وب هم نمایش داده می‌شود.

### Marzban — فقط با PasarGuard از قبل نصب‌شده

طبق [مستند رسمی](https://docs.pasarguard.org/en/migration/marzban/):

1. **ابتدا PasarGuard را دستی نصب کنید** (دیتابیس را در نصب انتخاب کنید)
2. در ویزارد بکاپ Marzban را آپلود کنید — **نوع DB مبدأ** (SQLite / MySQL) خودکار تشخیص داده می‌شود
3. در مرحله دیتابیس مقصد بپرسید: **با چه دیتابیسی PasarGuard را نصب کردید؟**
4. اگر DB مبدأ و مقصد متفاوت باشند (مثلاً Marzban SQLite → PasarGuard TimescaleDB)، تبدیل با `db-migrations` رسمی انجام می‌شود

- روش **درجا (in-place)** حذف شده — PasarGuard باید قبل از اجرای ویزارد نصب باشد
- Marzban می‌تواند روی همین سرور باشد؛ داده از بکاپ یا SQLite زنده خوانده می‌شود

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
2. **پنل مبدأ** — انتخاب + پیش‌نیازها (برای Marzban: PasarGuard باید نصب باشد)
3. **دیتابیس مبدأ** — برای Marzban: آپلود بکاپ + تشخیص خودکار DB؛ برای بقیه: انتخاب نوع DB
4. **دیتابیس مقصد** — برای Marzban: «با چه DBی PasarGuard را نصب کردید؟» + هشدار cross-DB
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
├── tests/
│   └── test_migration_logic.py  # تست منطق بدون Docker
├── app/
│   ├── main.py             # FastAPI backend
│   ├── panels.py           # ماتریس پنل‌ها + پیش‌نیازها (EN/FA/RU)
│   ├── models.py
│   ├── config.py
│   ├── services/
│   │   ├── prerequisites.py
│   │   ├── orchestrator.py
│   │   ├── upload.py
│   │   ├── db_migration.py      # ابزار مشترک db-migrations
│   │   └── migrators/
│   │       ├── marzban.py       # Marzban → PasarGuard (fresh install only)
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

### تست محلی (بدون سرور Ubuntu)

```bash
cd /opt/pg-migrator   # یا مسیر clone
python3 tests/test_migration_logic.py
```

این تست‌ها config تولیدی، پیشنهاد روش Marzban و import ماژول‌ها را بررسی می‌کنند. تست کامل end-to-end نیاز به سرور Ubuntu با Docker و Marzban واقعی دارد.

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

### v1.6.3
- رفع خطای `service "pasarguard" is not running` هنگام `alembic upgrade`
- اجرای Alembic با `compose run --entrypoint uv` (بدون استارت پنل)
- pull خودکار image و پشتیبانی از compose profiles

### v1.6.2
- رفع خودکار خطای `DuplicateColumnError` / `alembic_version` قبل از استارت PasarGuard
- `safe_start_pasarguard`: sync Alembic با one-shot + heal SQL + بررسی سلامت پنل
- فرم دستی credentials دیتابیس (بدون خواندن خودکار `.env`)
- همه migratorها از استارت امن PasarGuard استفاده می‌کنند

### v1.5.0
- Marzban: حذف روش **درجا (in-place)** — فقط مهاجرت با PasarGuard از قبل نصب‌شده
- مرحله ۲ Marzban: تشخیص خودکار DB مبدأ از بکاپ (بدون انتخاب دستی)
- مرحله ۳ Marzban: فقط «با چه دیتابیسی PasarGuard را نصب کردید؟» + هشدار cross-DB
- پشتیبانی دقیق مهاجرت بین DBهای مختلف (مثلاً SQLite → TimescaleDB)

### v1.2.0
- Marzban: دو روش رسمی — **درجا** (Marzban روی سرور) و **تازه** (PasarGuard + بکاپ)
- انتخاب روش در ویزارد بعد از انتخاب Marzban (EN/FA/RU)
- Cross-DB خودکار (مثلاً SQLite → TimescaleDB) با `db-migrations` مشترک
- رفع باگ `run_db_migration` و refactor `pasarguard_db.py`
- تست‌های validation در `tests/test_migration_logic.py`

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
