> ⚠️ **نسخه بتا (`v1.0beta`)** — عمومی منتشر شده، ولی هنوز آزمایشی است. احتمال باگ وجود دارد؛ با احتیاط استفاده کنید.

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# 🕒 PGClockMG

**ریستور و مهاجرت به PasarGuard**

`v1.0beta` · پورت `7000` · FA / EN / RU

</div>

---

## ⚠️ قبل از استفاده

| | |
|---|---|
| 🧪 **بتا** | این نسخه بتا است. روی سرور پروداکشن فقط با آگاهی از ریسک اجرا کنید. |
| 💾 **بکاپ** | **قبل از هر ریستور یا مهاجرت** از پنل، دیتابیس و فایل‌های مهم بکاپ کامل بگیرید. |
| 🛠️ **نصب پنل** | ویزارد **خودش PasarGuard نصب نمی‌کند**. اول پنل را نصب کنید، بعد برگردید. |

---

## ✨ چه کاری می‌کند؟

| هدف | توضیح |
|-----|--------|
| ♻️ **ریستور / تغییر DB** | بکاپ PasarGuard را برمی‌گرداند — حتی اگر نوع دیتابیس فرق کند |
| 🚚 **مهاجرت** | انتقال از Marzban، 3x-ui، Remnawave، Hiddify و … |
| 📘 **راهنما** | اگر پنل نصب است مشخصاتش را نشان می‌دهد؛ اگر نه، فقط دستور نصب رسمی |

مقصد ریستور همیشه دیتابیس **نصب‌شده** روی سرور است.

---

## 🚀 نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

باز کنید: **`http://SERVER_IP:7000`**

📋 نیازها: Ubuntu/Debian · root · Docker · پورت `7000`

---

## 📦 پنل‌ها

| مبدأ | وضعیت |
|------|--------|
| 🟢 Marzban | کامل |
| 🟢 PasarGuard (ریستور) | کامل |
| 🟡 3x-ui | جزئی |
| 🟠 Remnawave / Hiddify | آزمایشی |

---

## 🧰 دستورات

```bash
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# آپدیت به آخرین نسخه
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# بعد از اتمام کار
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## 🔒 حریم خصوصی

همه‌چیز فقط روی سرور شماست. بکاپ و رمزها جایی نمی‌روند.

---

## 📄 مجوز

**Copyright (c) 2026 Mrclocks — همه حقوق محفوظ است.**

نصب و استفادهٔ شخصی روی سرور خودتان آزاد است.  
کپی، بازنشر، فروش یا استفاده بدون اجازه / بدون ذکر نام **ممنوع** است و قابل پیگیری (از جمله DMCA در GitHub) می‌باشد.

جزئیات: [`LICENSE`](LICENSE) · ریپو: [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)
