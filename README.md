<div dir="rtl" align="right">

> 🚀 **`v4.3.3`** — بکاپ مستقل از تلگرام (بدون گیر کردن روی ۹۳٪ / Load failed)
> ⚠️ قبل از ریستور یا مهاجرت حتماً بکاپ کامل بگیرید.

<p align="center">
  <b>فارسی</b> · <a href="README.en.md">English</a> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# PGClockMG

🧰 ویزارد ریستور/مهاجرت + پنل بکاپ PasarGuard (نصب جدا)

</div>

---

## ✨ معرفی

| محصول | مسیر | پورت | سرویس |
|--------|------|------|--------|
| 🧭 **PGClockMG** (ویزارد) | `/opt/pg-migrator` | `7000` | `pg-migrator` |
| 💾 **PGClockBackup** | `/opt/pg-backup` | `7001` | `pg-backup` |

از `v4.0.1` این دو **جدا نصب و حذف می‌شوند**. پاک کردن ویزارد بعد از ریستور، بکاپر را حذف نمی‌کند.

> 📌 PasarGuard را خودتان نصب کنید؛ این ابزار پنل را نصب نمی‌کند.

---

## 📥 نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

### 🗂️ منوی اسکریپت

| گزینه | کار |
|-------|-----|
| ۱ · Install PGClockMG | فقط ویزارد — پورت وب را می‌پرسد |
| ۲ · Install PGClockBackup | فقط پنل بکاپ — پورت بکاپ را می‌پرسد و **توکن نصب یک‌بارمصرف** چاپ می‌کند |
| ۳ · Uninstall PGClockMG | فقط ویزارد را پاک می‌کند (بکاپر دست‌نخورده) |
| ۴ · Uninstall PGClockBackup | فقط پنل بکاپ را پاک می‌کند |
| ۵ · Redirect server | برای مهاجرت 3x-ui / Hiddify |
| ۶ · Exit | خروج |

### بدون سؤال

```bash
# فقط ویزارد
sudo PG_MIGRATOR_ACTION=install-wizard PG_MIGRATOR_PORT=7000 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# فقط بکاپ
sudo PG_MIGRATOR_ACTION=install-backup PG_BACKUP_PORT=7001 bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

---

## 🧭 آموزش ویزارد (PGClockMG)

آدرس: `http://SERVER_IP:7000/?token=...` · توکن از نصب‌کننده

### قابلیت‌ها
- ✅ ریستور بکاپ PasarGuard (حتی با تغییر DB)
- ✅ مهاجرت Marzban / 3x-ui / Hiddify / Remnawave
- ✅ دریافت استریم بکاپ و ریستور با تأیید دستی
- ✅ TimescaleDB heal خودکار

### مراحل
۱. بکاپ بگیرید → ۲. PasarGuard را روی سرور جدید نصب کنید → ۳. ویزارد را نصب کنید → ۴. ریستور/مهاجرت.

بعد از ریستور موفق می‌توانید از منو **Uninstall PGClockMG** بزنید؛ PGClockBackup اگر نصب باشد می‌ماند.

بازیابی توکن:
```bash
cat /opt/pg-migrator/.access_token
```

---

## 💾 آموزش پنل بکاپ (PGClockBackup)

آدرس: `http://SERVER_IP:7001/` · در اولین ورود: **توکن نصب** + رمز قوی

### قابلیت‌ها
- 📦 بکاپ کامل از `/opt/pasarguard` + `/var/lib/pasarguard`
- 🗄️ SQLite / PostgreSQL / TimescaleDB / MySQL / MariaDB
- 📊 داشبورد سلامت و آمار
- ⏰ زمان‌بندی هر ۱ / ۳ / ۶ / ۱۲ / ۲۴ ساعت (با منطقهٔ زمانی) + نگه‌داشت بر اساس تعداد و روز
- ✅ تأیید سلامت بکاپ (SHA256 + CRC) بعد از ساخت
- 📣 اعلان شکست زمان‌بندی (تلگرام / وب‌هوک)
- 📱 تلگرام چندمقصدی (+ Topic) با پروکسی و تگ متصل/قطع
- 🌊 استریم به ویزارد مقصد از دکمهٔ لیست بکاپ → تأیید دستی → ریستور
- 🔐 رمز جدا، سشن جدا، مسیر نصب جدا از ویزارد

### امنیت (خلاصه)
- توکن یک‌بارمصرف برای اولین رمز
- محدودیت تلاش ورود
- چرخش سشن با تغییر رمز
- مسدودسازی SSRF روی استریم/پروکسی
- بدون OpenAPI عمومی روی پنل بکاپ

بازیابی توکن نصب (تا قبل از اولین ورود):
```bash
cat /opt/pg-backup/backup_panel/.setup_token
```

---

## 🛠️ دستورات

```bash
systemctl status pg-migrator
systemctl status pg-backup
journalctl -u pg-migrator -f
journalctl -u pg-backup -f

# به‌روزرسانی: دوباره منو → گزینه ۱ یا ۲
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

نقطهٔ ریستور قبل از خانوادهٔ ۴: `restore-point-pre-v4.0.0`

---

## 📄 مجوز

**Copyright (c) 2026 Mrclocks — همه حقوق محفوظ است.**

[`LICENSE`](LICENSE) · [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)

</div>
