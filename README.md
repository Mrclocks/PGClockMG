<div dir="rtl" align="right">

> ⚠️ **نسخه بتا (`v1.0beta`)** — الان عمومی شده، ولی هنوز در مرحله آزمایش است. ممکن است باگ داشته باشد؛ لطفاً با احتیاط استفاده کنید.

<p align="center">
  <b>فارسی</b> · <a href="README.en.md">English</a> · <a href="README.ru.md">Русский</a>
</p>

<div align="center">

<img src="preview.png" alt="PGClockMG" width="720">

# 🕒 PGClockMG

**ریستور و مهاجرت به PasarGuard**

`v1.0beta` · پورت `7000` · فارسی / English / Русский

</div>

---

## ⚠️ قبل از شروع

| | |
|---|---|
| 🧪 **نسخه بتا** | این نسخه هنوز کامل تثبیت نشده. اگر روی سرور اصلی کار می‌کنید، ریسک را در نظر بگیرید. |
| 💾 **حتماً بکاپ بگیرید** | قبل از ریستور یا مهاجرت، از پنل، دیتابیس و فایل‌های مهم یک بکاپ کامل بگیرید. |
| 🛠️ **نصب پنل** | این ویزارد خودش PasarGuard را نصب نمی‌کند. اول پنل را نصب کنید، بعد برای ریستور یا مهاجرت برگردید. |

---

## ✨ این ابزار چه کار می‌کند؟

| کار | توضیح |
|-----|--------|
| ♻️ **ریستور / تغییر دیتابیس** | بکاپ PasarGuard را برمی‌گرداند؛ حتی اگر نوع دیتابیس با قبل فرق داشته باشد |
| 🚚 **مهاجرت** | داده‌ها را از پنل‌هایی مثل Marzban، 3x-ui، Remnawave و Hiddify منتقل می‌کند |
| 📘 **راهنما** | اگر پنل نصب باشد مشخصاتش را نشان می‌دهد؛ اگر نباشد فقط دستور نصب رسمی را می‌بینید |

نکته: مقصد ریستور همیشه همان دیتابیسی است که الان روی سرور نصب شده.

---

## 🚀 نصب

```bash
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"
```

بعد از نصب این آدرس را باز کنید: **`http://SERVER_IP:7000`**

📋 پیش‌نیازها: Ubuntu یا Debian · دسترسی root · Docker · پورت `7000`

---

## 📦 پشتیبانی از پنل‌ها

| مبدأ | وضعیت |
|------|--------|
| 🟢 Marzban | کامل |
| 🟢 PasarGuard (ریستور) | کامل |
| 🟡 3x-ui | جزئی |
| 🟠 Remnawave / Hiddify | آزمایشی |

---

## 🧰 دستورات کاربردی

```bash
systemctl status pg-migrator
systemctl restart pg-migrator
journalctl -u pg-migrator -f

# به‌روزرسانی به آخرین نسخه
sudo bash -c "$(curl -fsSL 'https://raw.githubusercontent.com/Mrclocks/PGClockMG/main/install.sh?v='$(date +%s))"

# وقتی کارتان تمام شد
systemctl stop pg-migrator && systemctl disable pg-migrator
```

---

## 🔒 حریم خصوصی

همه‌چیز فقط روی سرور خودتان اجرا می‌شود. بکاپ‌ها و رمزها جایی خارج از سرور شما ارسال نمی‌شوند.

---

## 📄 مجوز

**Copyright (c) 2026 Mrclocks — همه حقوق محفوظ است.**

نصب و استفاده شخصی روی سرور خودتان آزاد است.  
کپی، بازنشر، فروش یا استفاده بدون اجازه و بدون ذکر نام **مجاز نیست** و قابل پیگیری است (از جمله از طریق DMCA در GitHub).

جزئیات بیشتر: [`LICENSE`](LICENSE) · مخزن: [github.com/Mrclocks/PGClockMG](https://github.com/Mrclocks/PGClockMG)

</div>
