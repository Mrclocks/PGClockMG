> ⚠️ **این تنها یک نسخه آزمایشی می‌باشد و احتمال وجود باگ زیاد است.**

<div align="center">

<img src="preview.png" alt="MrClock-MG Wizard Preview" width="720">

# MrClock-MG

**مهاجرت پنل‌های VPN به PasarGuard — ویزارد وب روی سرور خودتان**

`v2.3.8` · پورت `7000` · FA / EN / RU

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

1. **خوش‌آمد** → شروع
2. **نصب / تشخیص PasarGuard** — اگر نصب نبود، نصب خودکار با انتخاب دیتابیس و SSL (دامنه یا IP)
3. **ادامه کار:** پایان و ورود · ریستور بکاپ PasarGuard · مهاجرت از پنل‌های دیگر

> اگر قصد ریستور بکاپ دارید، **نوع دیتابیس نصب باید با بکاپ یکی باشد.**

---

## نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

سپس: **`http://SERVER_IP:7000`**

**پیش‌نیاز:** Ubuntu/Debian · root · Docker · پورت 7000 آزاد

نصب PasarGuard از داخل ویزارد از اسکریپت رسمی [PasarGuard/panel](https://github.com/PasarGuard/panel) استفاده می‌کند (نود نصب نمی‌شود؛ در پایان راهنمای نود داده می‌شود).

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

## امنیت

- ویزارد **بدون SSL** روی پورت 7000 است — با فایروال فقط IP خودتان را مجاز کنید.
- حتماً **قبل از مهاجرت / ریستور بکاپ** بگیرید.
- رمز دیتابیس فقط در حافظه همان session ویزارد استفاده می‌شود.

---

<div align="center">

**MIT** — استفاده با مسئولیت خودتان

</div>
