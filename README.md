> ⚠️ **این تنها یک نسخه آزمایشی می‌باشد و احتمال وجود باگ زیاد است.**

<div align="center">

<img src="preview.png" alt="MrClock-MG Wizard Preview" width="720">

# MrClock-MG

**مهاجرت پنل‌های VPN به PasarGuard — ویزارد وب روی سرور خودتان**

`v2.8.0` · پورت `7000` · FA / EN / RU

</div>

---

## حریم خصوصی

> **همه مراحل روی سرور خود شما اجرا می‌شود.**
>
> بکاپ، دیتابیس، رمزها و لاگ‌ها از سرور خارج نمی‌شوند. ویزارد فقط روی `localhost` / IP سرور شما گوش می‌دهد و مستقیماً با Docker و دیتابیس محلی کار می‌کند.

| | |
|---|---|
| داده به ابر ارسال نمی‌شود | مهاجرت local است |
| API خارجی برای DB ندارد | فقط سرور شما |
| بعد از اتمام کار سرویس را خاموش کنید | `systemctl stop pg-migrator` |

---

## فلو ویزارد

1. **خوش‌آمد** → انتخاب هدف
2. **راهنما / وضعیت پنل** — اگر PasarGuard نصب است مشخصاتش را می‌بینید؛ اگر نه، فقط دستور نصب رسمی + آموزش Owner (ویزارد خودش چیزی نصب نمی‌کند)
3. **ادامه کار:** ریستور بکاپ / تغییر DB · مهاجرت از پنل‌های دیگر

> **پیش‌نیاز ریستور و مهاجرت:** PasarGuard از قبل روی سرور نصب باشد. اگر نصب نباشد، مودال شما را به تب راهنما می‌برد.

### قوانین تبدیل دیتابیس

| مبدأ → مقصد | وضعیت |
|-------------|--------|
| SQLite → هر موتور | ✅ |
| MySQL / MariaDB / PostgreSQL / Timescale ↔ یکدیگر | ✅ |
| هر موتور غیر SQLite → SQLite | ❌ |

---

## نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

سپس: **`http://SERVER_IP:7000`**

**پیش‌نیاز:** Ubuntu/Debian · root · Docker · پورت 7000 آزاد

نصب خود PasarGuard را خودتان با دستورهای رسمی در تب راهنمای ویزارد (از [مستندات PasarGuard](https://docs.pasarguard.org/en/panel/installation/)) انجام دهید.

---

## پنل‌های پشتیبانی‌شده

| مبدأ | وضعیت | لینک اشتراک |
|------|--------|-------------|
| **Marzban** | کامل | حفظ می‌شود |
| **3x-ui** | جزئی | با redirect server |
| **PasarGuard** | تغییر DB / ریستور بکاپ | حفظ می‌شود |
| Remnawave | آزمایشی | تغییر می‌کند |
| Hiddify | آزمایشی | تغییر می‌کند |

---

## دستورات

```bash
systemctl status pg-migrator          # وضعیت
systemctl restart pg-migrator         # ری‌استارت ویزارد
journalctl -u pg-migrator -f          # لاگ سرویس

# آپدیت به آخرین نسخه
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# بعد از مهاجرت موفق — خاموش کردن ویزارد
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## لایسنس

MIT
