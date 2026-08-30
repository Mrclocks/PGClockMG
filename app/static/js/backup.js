/* PGClockMG Backup panel client */

const ICONS = {
  shield: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M12 3l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V7l8-4z"/></svg>',
  db: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></svg>',
  docker: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="3" y="10" width="4" height="4" rx="0.5"/><rect x="8" y="10" width="4" height="4" rx="0.5"/><rect x="13" y="10" width="4" height="4" rx="0.5"/><rect x="8" y="5" width="4" height="4" rx="0.5"/><path d="M3 16h14a4 4 0 0 0 4-4"/></svg>',
  disk: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="4" y="4" width="16" height="16" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M4 9h16"/></svg>',
  cpu: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"/></svg>',
  archive: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M4 7h16v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7z"/><path d="M3 7l1.5-3h15L21 7"/><path d="M10 12h4"/></svg>',
  users: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="9.5" cy="7" r="3.5"/><path d="M22 21v-2a3.5 3.5 0 0 0-2.5-3.3"/><path d="M16.5 3.7a3.5 3.5 0 0 1 0 6.6"/></svg>',
  nodes: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8 7.5l3.2 8M16 7.5l-3.2 8"/></svg>',
  admins: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M12 3l7 4v5c0 4.2-2.8 7.2-7 8.8C7.8 19.2 5 16.2 5 12V7l7-4z"/><path d="M9.5 12l1.8 1.8L15 10"/></svg>',
  inbounds: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M4 12h12"/><path d="M12 6l6 6-6 6"/><path d="M20 5v14"/></svg>',
  hosts: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="3" y="4" width="18" height="8" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 8h.01M7 17h.01"/></svg>',
  groups: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M8 10h8v10H8z"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>',
  clock: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  send: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M22 3L11 14"/><path d="M22 3l-7 19-3-8-8-3 18-8z"/></svg>',
  stream: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M16 3h5v5"/><path d="M4 20L21 3"/><path d="M21 16v5h-5"/></svg>',
  warn: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M12 3l9 16H3L12 3z"/><path d="M12 10v4"/><path d="M12 17h.01"/></svg>',
  empty: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M4 7h16v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7z"/><path d="M3 7l1.5-3h15L21 7"/></svg>',
};

const I18N = {
  fa: {
    logout: "خروج",
    tabDash: "داشبورد",
    tabList: "بکاپ‌ها",
    tabSettings: "تنظیمات",
    authSetupTitle: "رمز پنل بکاپ",
    authSetupDesc: "اولین ورود: یک رمز قوی برای پنل بکاپ بگذارید.",
    lblSetupToken: "توکن نصب (یک‌بارمصرف)",
    authSetupTokenHint: "همان توکنی که نصب‌کننده در پایان چاپ کرد.",
    lblCurrentPass: "رمز فعلی",
    errSetupToken: "توکن نصب نامعتبر است. همان توکن چاپ‌شده توسط نصب‌کننده را بدون فاصله کپی کنید.",
    errPassMismatch: "رمز و تکرار آن یکی نیست.",
    errPassSet: "رمز قبلاً تنظیم شده — از ورود استفاده کنید.",
    errWeakPass: "رمز ضعیف است. حداقل ۱۲ کاراکتر با حرف بزرگ، کوچک، عدد و نماد.",
    errBadPass: "رمز اشتباه است.",
    errThrottle: "تلاش‌های زیاد — چند دقیقه صبر کنید.",
    errServer: "خطای سرور. لاگ سرویس را ببینید: journalctl -u pg-backup -n 50",
    authLoginTitle: "ورود به پنل بکاپ",
    authLoginDesc: "با رمز پنل بکاپ وارد شوید.",
    lblPassword: "رمز عبور",
    lblPasswordConfirm: "تکرار رمز",
    authPolicy: "حداقل ۱۲ کاراکتر، شامل حرف بزرگ، کوچک، عدد و نماد.",
    btnSetup: "ذخیره و ورود",
    btnLogin: "ورود",
    dashH2: "پایش و بکاپ",
    dashDesc: "وضعیت سیستم و بکاپ کامل با یک کلیک.",
    dashPgTitle: "وضعیت PasarGuard",
    dashPgSubOk: "پنل روی این سرور نصب است و آماده بکاپ‌گیری است.",
    dashPgSubMissing: "PasarGuard روی این سرور پیدا نشد — اول پنل را نصب کنید.",
    secHealthTitle: "سلامت سرور و بکاپ",
    secPgTitle: "جزئیات پنل",
    secStatsTitle: "آمار زنده پنل",
    secBackupStatusTitle: "وضعیت بکاپ و ارسال",
    btnBackupNow: "بکاپ کامل همین حالا",
    backupRunning: "در حال ساخت بکاپ…",
    backupDone: "بکاپ آماده شد",
    backupFail: "بکاپ ناموفق بود",
    listH2: "فایل‌های بکاپ",
    listDesc: "دانلود، ارسال به تلگرام، یا استریم به سرور مقصد.",
    emptyList: "هنوز بکاپی نیست. از داشبورد یک بکاپ کامل بگیرید.",
    download: "دانلود",
    sendTg: "تلگرام",
    sendStream: "استریم",
    remove: "حذف",
    setH2: "تنظیمات",
    setDesc: "هر بخش جداگانه مرتب شده؛ فقط همان چیزی که لازم دارید را عوض کنید.",
    setSchedTitle: "زمان‌بندی خودکار",
    setSchedHint: "بکاپ روزانه بر اساس ساعت UTC.",
    lblSchedEnabled: "بکاپ خودکار روزانه (UTC)",
    lblSchedHour: "ساعت",
    lblSchedMinute: "دقیقه",
    lblRetention: "تعداد نگه‌داری",
    lblSchedTelegram: "بعد از بکاپ خودکار به تلگرام هم بفرست",
    setTgTitle: "تلگرام",
    setTgHint: "اختیاری — فایل روی سرور تک‌تکه می‌ماند؛ برای تلگرام در صورت نیاز تکه می‌شود.",
    lblTgEnabled: "ارسال به تلگرام فعال باشد",
    lblTgToken: "Bot Token",
    lblTgChat: "Chat ID",
    lblTgCaption: "متن پیام",
    tgCaptionHint: "متغیرها: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgPreview: "پیش‌نمایش متن",
    btnTgTest: "تست اتصال",
    setProxyTitle: "پروکسی تلگرام",
    setProxyHint: "اگر تلگرام مستقیم در دسترس نیست.",
    lblProxyEnabled: "از پروکسی استفاده کن",
    lblProxyType: "نوع",
    lblProxyHost: "هاست",
    lblProxyPort: "پورت",
    lblProxyUser: "کاربر",
    lblProxyPass: "رمز پروکسی",
    setStreamTitle: "مقصد استریم پیش‌فرض",
    lblStreamDest: "آدرس ویزارد مقصد",
    streamDestHint: "۱) روی سرور مقصد ویزارد را باز کنید → ریستور → «آماده‌سازی دریافت استریم». ۲) توکن را کپی کنید. ۳) اینجا آدرس همان سرور (مثلاً http://IP:7000) و توکن را بزنید و ارسال کنید.",
    setPassTitle: "تغییر رمز پنل",
    setPassHint: "خالی بگذارید اگر نمی‌خواهید عوض شود.",
    lblNewPass: "رمز جدید",
    lblNewPassConfirm: "تکرار رمز جدید",
    btnSaveSettings: "ذخیره تنظیمات",
    saved: "ذخیره شد",
    streamH2: "ارسال استریم به ویزارد",
    streamDesc: "بکاپ روی همین سرور می‌ماند و فقط یک کپی به ویزارد مقصد فرستاده می‌شود. اول مقصد را در حالت دریافت بگذارید، بعد از اینجا ارسال کنید.",
    streamStep1: "روی سرور مقصد: ویزارد → ریستور → آماده‌سازی دریافت استریم",
    streamStep2: "توکن یک‌بارمصرف را کپی کنید (حدود ۳۰ دقیقه معتبر است)",
    streamStep3: "اینجا آدرس ویزارد مقصد و توکن را وارد کنید و ارسال را بزنید",
    streamConnTitle: "وضعیت ارسال",
    streamUrlHint: "مثلاً http://203.0.113.10:7000 — همان پورتی که ویزارد مقصد روی آن گوش می‌دهد.",
    streamTokenHint: "توکنی که دکمه «آماده‌سازی دریافت استریم» روی ویزارد مقصد نشان می‌دهد.",
    streamIdle: "آماده",
    streamConnecting: "در حال اتصال به مقصد…",
    streamSending: "در حال ارسال…",
    streamSuccess: "ارسال کامل شد",
    streamFail: "ارسال ناموفق",
    lblStreamUrl: "آدرس ویزارد مقصد",
    lblStreamToken: "توکن دریافت از ویزارد مقصد",
    btnStreamSend: "شروع ارسال",
    btnStreamBack: "بازگشت",
    users: "کاربر",
    nodes: "نود",
    admins: "ادمین",
    inbounds: "اینباند",
    hosts: "هاست",
    groups: "گروه",
    db: "دیتابیس",
    lastBackup: "آخرین بکاپ",
    noLast: "هنوز بکاپی گرفته نشده",
    size: "حجم",
    confirmDelete: "این بکاپ حذف شود؟",
    healthPg: "PasarGuard",
    healthDocker: "Docker",
    healthDisk: "فضای بکاپ",
    healthMem: "رم آزاد",
    healthCpu: "CPU / Load",
    healthArchives: "آرشیوها",
    freeOf: "آزاد از",
    totalSize: "حجم کل",
    keepLast: "نگه‌داری",
    scheduleOn: "زمان‌بندی روشن",
    scheduleOff: "زمان‌بندی خاموش",
    telegramOn: "تلگرام فعال",
    telegramOff: "تلگرام خاموش",
    telegramReady: "پیکربندی کامل",
    telegramNeedConfig: "نیاز به تنظیم",
    tgConnected: "متصل",
    tgDisconnected: "قطع",
    tgChecking: "در حال بررسی…",
    proxyOn: "پروکسی روشن",
    streamSet: "مقصد استریم ست شده",
    streamUnset: "مقصد استریم خالی",
    panelUrl: "آدرس پنل",
    ssl: "SSL",
    port: "پورت",
    yes: "بله",
    no: "خیر",
    lastError: "آخرین خطا",
    deliveryTitle: "ارسال و زمان‌بندی",
    profile: "پروفایل منابع",
  },
  en: {
    logout: "Logout",
    tabDash: "Dashboard",
    tabList: "Backups",
    tabSettings: "Settings",
    authSetupTitle: "Backup panel password",
    authSetupDesc: "First run: set a strong password for the backup panel.",
    lblSetupToken: "Install setup token (one-time)",
    authSetupTokenHint: "Use the token printed by the installer at the end.",
    lblCurrentPass: "Current password",
    errSetupToken: "Invalid setup token. Paste the installer token exactly, with no spaces.",
    errPassMismatch: "Password and confirmation do not match.",
    errPassSet: "Password already set — use login.",
    errWeakPass: "Weak password. Use 12+ chars with upper, lower, digit, and symbol.",
    errBadPass: "Wrong password.",
    errThrottle: "Too many attempts — wait a few minutes.",
    errServer: "Server error. Check: journalctl -u pg-backup -n 50",
    authLoginTitle: "Backup panel login",
    authLoginDesc: "Sign in with your backup panel password.",
    lblPassword: "Password",
    lblPasswordConfirm: "Confirm password",
    authPolicy: "At least 12 chars, with upper, lower, digit, and symbol.",
    btnSetup: "Save & enter",
    btnLogin: "Sign in",
    dashH2: "Health & backup",
    dashDesc: "System health and one-click full backup.",
    dashPgTitle: "PasarGuard status",
    dashPgSubOk: "Panel is installed on this server and ready for backup.",
    dashPgSubMissing: "PasarGuard was not found — install the panel first.",
    secHealthTitle: "Server & backup health",
    secPgTitle: "Panel details",
    secStatsTitle: "Live panel stats",
    secBackupStatusTitle: "Backup & delivery status",
    btnBackupNow: "Create full backup now",
    backupRunning: "Creating backup…",
    backupDone: "Backup ready",
    backupFail: "Backup failed",
    listH2: "Backup files",
    listDesc: "Download, send to Telegram, or stream to a destination server.",
    emptyList: "No backups yet. Create a full backup from the dashboard.",
    download: "Download",
    sendTg: "Telegram",
    sendStream: "Stream",
    remove: "Delete",
    setH2: "Settings",
    setDesc: "Clear sections — change only what you need.",
    setSchedTitle: "Automatic schedule",
    setSchedHint: "Daily backup using UTC clock.",
    lblSchedEnabled: "Daily automatic backup (UTC)",
    lblSchedHour: "Hour",
    lblSchedMinute: "Minute",
    lblRetention: "Keep last N",
    lblSchedTelegram: "Also send scheduled backups to Telegram",
    setTgTitle: "Telegram",
    setTgHint: "Optional — kept as one file on disk; split only for Telegram upload.",
    lblTgEnabled: "Enable Telegram delivery",
    lblTgToken: "Bot Token",
    lblTgChat: "Chat ID",
    lblTgCaption: "Message text",
    tgCaptionHint: "Vars: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgPreview: "Preview text",
    btnTgTest: "Test connection",
    setProxyTitle: "Telegram proxy",
    setProxyHint: "Use when Telegram is blocked directly.",
    lblProxyEnabled: "Use proxy",
    lblProxyType: "Type",
    lblProxyHost: "Host",
    lblProxyPort: "Port",
    lblProxyUser: "User",
    lblProxyPass: "Proxy password",
    setStreamTitle: "Default stream destination",
    lblStreamDest: "Destination wizard URL",
    streamDestHint: "1) On destination: open wizard → Restore → Ready to receive stream. 2) Copy the one-time token. 3) Paste the wizard URL (e.g. http://IP:7000) and token here, then send.",
    setPassTitle: "Change panel password",
    setPassHint: "Leave blank to keep the current password.",
    lblNewPass: "New password",
    lblNewPassConfirm: "Confirm new password",
    btnSaveSettings: "Save settings",
    saved: "Saved",
    streamH2: "Stream to wizard",
    streamDesc: "The zip stays on this server; a copy is pushed to the destination wizard. Put the destination in receive mode first, then send from here.",
    streamStep1: "On destination: Wizard → Restore → Ready to receive stream",
    streamStep2: "Copy the one-time token (valid ~30 minutes)",
    streamStep3: "Paste destination wizard URL + token here and start send",
    streamConnTitle: "Send status",
    streamUrlHint: "Example: http://203.0.113.10:7000 — the port where the destination wizard listens.",
    streamTokenHint: "Token shown after clicking Ready to receive stream on the destination wizard.",
    streamIdle: "Ready",
    streamConnecting: "Connecting to destination…",
    streamSending: "Sending…",
    streamSuccess: "Send complete",
    streamFail: "Send failed",
    lblStreamUrl: "Destination wizard URL",
    lblStreamToken: "Receive token from destination wizard",
    btnStreamSend: "Start send",
    btnStreamBack: "Back",
    users: "Users",
    nodes: "Nodes",
    admins: "Admins",
    inbounds: "Inbounds",
    hosts: "Hosts",
    groups: "Groups",
    db: "Database",
    lastBackup: "Last backup",
    noLast: "No backup yet",
    size: "Size",
    confirmDelete: "Delete this backup?",
    healthPg: "PasarGuard",
    healthDocker: "Docker",
    healthDisk: "Backup disk",
    healthMem: "Free RAM",
    healthCpu: "CPU / Load",
    healthArchives: "Archives",
    freeOf: "free of",
    totalSize: "Total size",
    keepLast: "Retention",
    scheduleOn: "Schedule on",
    scheduleOff: "Schedule off",
    telegramOn: "Telegram on",
    telegramOff: "Telegram off",
    telegramReady: "Configured",
    telegramNeedConfig: "Needs setup",
    tgConnected: "Connected",
    tgDisconnected: "Disconnected",
    tgChecking: "Checking…",
    proxyOn: "Proxy on",
    streamSet: "Stream destination set",
    streamUnset: "No stream destination",
    panelUrl: "Panel URL",
    ssl: "SSL",
    port: "Port",
    yes: "Yes",
    no: "No",
    lastError: "Last error",
    deliveryTitle: "Delivery & schedule",
    profile: "Resource profile",
  },
  ru: {
    logout: "Выход",
    tabDash: "Панель",
    tabList: "Бэкапы",
    tabSettings: "Настройки",
    authSetupTitle: "Пароль панели бэкапа",
    authSetupDesc: "Первый запуск: задайте надёжный пароль.",
    lblSetupToken: "Токен установки (одноразовый)",
    authSetupTokenHint: "Тот же токен, который установщик напечатал в конце.",
    lblCurrentPass: "Текущий пароль",
    errSetupToken: "Неверный токен установки. Вставьте токен установщика без пробелов.",
    errPassMismatch: "Пароль и подтверждение не совпадают.",
    errPassSet: "Пароль уже задан — войдите.",
    errWeakPass: "Слабый пароль. Минимум 12 символов: верхний/нижний регистр, цифра, спецсимвол.",
    errBadPass: "Неверный пароль.",
    errThrottle: "Слишком много попыток — подождите несколько минут.",
    errServer: "Ошибка сервера. Смотрите: journalctl -u pg-backup -n 50",
    authLoginTitle: "Вход в панель бэкапа",
    authLoginDesc: "Войдите с паролем панели бэкапа.",
    lblPassword: "Пароль",
    lblPasswordConfirm: "Повтор пароля",
    authPolicy: "Минимум 12 символов: заглавные, строчные, цифра и спецсимвол.",
    btnSetup: "Сохранить и войти",
    btnLogin: "Войти",
    dashH2: "Мониторинг и бэкап",
    dashDesc: "Здоровье системы и полный бэкап в один клик.",
    dashPgTitle: "Статус PasarGuard",
    dashPgSubOk: "Панель установлена и готова к бэкапу.",
    dashPgSubMissing: "PasarGuard не найден — сначала установите панель.",
    secHealthTitle: "Здоровье сервера и бэкапа",
    secPgTitle: "Детали панели",
    secStatsTitle: "Живая статистика",
    secBackupStatusTitle: "Статус бэкапа и доставки",
    btnBackupNow: "Сделать полный бэкап",
    backupRunning: "Создание бэкапа…",
    backupDone: "Бэкап готов",
    backupFail: "Ошибка бэкапа",
    listH2: "Файлы бэкапов",
    listDesc: "Скачать, отправить в Telegram или стримом на сервер.",
    emptyList: "Бэкапов пока нет. Создайте полный бэкап на панели.",
    download: "Скачать",
    sendTg: "Telegram",
    sendStream: "Стрим",
    remove: "Удалить",
    setH2: "Настройки",
    setDesc: "Разделы по делу — меняйте только нужное.",
    setSchedTitle: "Авторасписание",
    setSchedHint: "Ежедневный бэкап по UTC.",
    lblSchedEnabled: "Ежедневный бэкап (UTC)",
    lblSchedHour: "Час",
    lblSchedMinute: "Минута",
    lblRetention: "Хранить N",
    lblSchedTelegram: "Также слать в Telegram",
    setTgTitle: "Telegram",
    setTgHint: "Опционально — на диске один файл; дробление только для Telegram.",
    lblTgEnabled: "Включить Telegram",
    lblTgToken: "Bot Token",
    lblTgChat: "Chat ID",
    lblTgCaption: "Текст сообщения",
    tgCaptionHint: "Переменные: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgPreview: "Превью",
    btnTgTest: "Тест",
    setProxyTitle: "Прокси Telegram",
    setProxyHint: "Если Telegram недоступен напрямую.",
    lblProxyEnabled: "Использовать прокси",
    lblProxyType: "Тип",
    lblProxyHost: "Хост",
    lblProxyPort: "Порт",
    lblProxyUser: "Пользователь",
    lblProxyPass: "Пароль прокси",
    setStreamTitle: "Назначение стрима",
    lblStreamDest: "URL мастера назначения",
    streamDestHint: "1) На назначении: мастер → Restore → Готов принимать стрим. 2) Скопируйте одноразовый токен. 3) Вставьте URL мастера (например http://IP:7000) и токен здесь, затем отправьте.",
    setPassTitle: "Смена пароля",
    setPassHint: "Оставьте пустым, чтобы не менять.",
    lblNewPass: "Новый пароль",
    lblNewPassConfirm: "Повтор",
    btnSaveSettings: "Сохранить",
    saved: "Сохранено",
    streamH2: "Стрим в мастер",
    streamDesc: "ZIP остаётся здесь; копия уходит на мастер назначения. Сначала включите приём на назначении, потом отправляйте отсюда.",
    streamStep1: "На назначении: Мастер → Restore → Готов принимать стрим",
    streamStep2: "Скопируйте одноразовый токен (действует ~30 минут)",
    streamStep3: "Вставьте URL мастера и токен здесь и начните отправку",
    streamConnTitle: "Статус отправки",
    streamUrlHint: "Пример: http://203.0.113.10:7000 — порт, на котором слушает мастер назначения.",
    streamTokenHint: "Токен после кнопки «Готов принимать стрим» на мастере назначения.",
    streamIdle: "Готово",
    streamConnecting: "Подключение к назначению…",
    streamSending: "Отправка…",
    streamSuccess: "Отправка завершена",
    streamFail: "Отправка не удалась",
    lblStreamUrl: "URL мастера назначения",
    lblStreamToken: "Токен приёма с мастера назначения",
    btnStreamSend: "Отправить",
    btnStreamBack: "Назад",
    users: "Пользователи",
    nodes: "Ноды",
    admins: "Админы",
    inbounds: "Inbounds",
    hosts: "Hosts",
    groups: "Groups",
    db: "БД",
    lastBackup: "Последний бэкап",
    noLast: "Пока нет",
    size: "Размер",
    confirmDelete: "Удалить бэкап?",
    healthPg: "PasarGuard",
    healthDocker: "Docker",
    healthDisk: "Диск бэкапа",
    healthMem: "Свободная RAM",
    healthCpu: "CPU / Load",
    healthArchives: "Архивы",
    freeOf: "свободно из",
    totalSize: "Общий размер",
    keepLast: "Хранение",
    scheduleOn: "Расписание вкл.",
    scheduleOff: "Расписание выкл.",
    telegramOn: "Telegram вкл.",
    telegramOff: "Telegram выкл.",
    telegramReady: "Настроено",
    telegramNeedConfig: "Нужна настройка",
    tgConnected: "Подключено",
    tgDisconnected: "Нет связи",
    tgChecking: "Проверка…",
    proxyOn: "Прокси вкл.",
    streamSet: "Назначение задано",
    streamUnset: "Назначение пусто",
    panelUrl: "URL панели",
    ssl: "SSL",
    port: "Порт",
    yes: "Да",
    no: "Нет",
    lastError: "Последняя ошибка",
    deliveryTitle: "Доставка и расписание",
    profile: "Профиль ресурсов",
  },
};

let lang = localStorage.getItem("pg_backup_lang") || "fa";
let setupMode = false;
let pollTimer = null;

function t(key) {
  return (I18N[lang] && I18N[lang][key]) || (I18N.en[key] || key);
}

function setBackupLang(next) {
  lang = next;
  localStorage.setItem("pg_backup_lang", next);
  document.documentElement.lang = next;
  document.documentElement.dir = next === "fa" ? "rtl" : "ltr";
  document.querySelectorAll(".lang-btn").forEach((btn) => {
    btn.setAttribute("aria-pressed", btn.dataset.lang === next ? "true" : "false");
  });
  applyI18n();
  if (!document.getElementById("panel-auth")?.classList.contains("active")) {
    refreshDashboard().catch(() => {});
    if (document.getElementById("panel-list")?.classList.contains("active")) {
      refreshList().catch(() => {});
    }
  }
}

function applyI18n() {
  const map = [
    ["tabDash", "tabDash"], ["tabList", "tabList"], ["tabSettings", "tabSettings"],
    ["btnLogout", "logout"], ["lblPassword", "lblPassword"], ["lblPasswordConfirm", "lblPasswordConfirm"],
    ["authPolicy", "authPolicy"], ["dashH2", "dashH2"], ["dashDesc", "dashDesc"], ["dashPgTitle", "dashPgTitle"],
    ["secHealthTitle", "secHealthTitle"], ["secPgTitle", "secPgTitle"],
    ["secBackupStatusTitle", "secBackupStatusTitle"],
    ["btnBackupNow", "btnBackupNow"], ["btnBackupNowList", "btnBackupNow"],
    ["listH2", "listH2"], ["listDesc", "listDesc"],
    ["setH2", "setH2"], ["setDesc", "setDesc"], ["setSchedTitle", "setSchedTitle"], ["setSchedHint", "setSchedHint"],
    ["lblSchedHour", "lblSchedHour"], ["lblSchedMinute", "lblSchedMinute"],
    ["lblRetention", "lblRetention"], ["lblSchedTelegram", "lblSchedTelegram"],
    ["setTgTitle", "setTgTitle"], ["setTgHint", "setTgHint"], ["lblTgToken", "lblTgToken"],
    ["lblTgChat", "lblTgChat"], ["lblTgCaption", "lblTgCaption"], ["tgCaptionHint", "tgCaptionHint"],
    ["btnTgPreview", "btnTgPreview"], ["btnTgTest", "btnTgTest"],
    ["setProxyTitle", "setProxyTitle"], ["setProxyHint", "setProxyHint"],
    ["lblProxyType", "lblProxyType"], ["lblProxyHost", "lblProxyHost"], ["lblProxyPort", "lblProxyPort"],
    ["lblProxyUser", "lblProxyUser"], ["lblProxyPass", "lblProxyPass"],
    ["setStreamTitle", "setStreamTitle"], ["lblStreamDest", "lblStreamDest"], ["streamDestHint", "streamDestHint"],
    ["setPassTitle", "setPassTitle"], ["setPassHint", "setPassHint"],
    ["lblCurrentPass", "lblCurrentPass"], ["lblNewPass", "lblNewPass"], ["lblNewPassConfirm", "lblNewPassConfirm"],
    ["lblSetupToken", "lblSetupToken"], ["authSetupTokenHint", "authSetupTokenHint"],
    ["btnSaveSettings", "btnSaveSettings"],
    ["streamH2", "streamH2"], ["streamDesc", "streamDesc"], ["lblStreamUrl", "lblStreamUrl"],
    ["lblStreamToken", "lblStreamToken"], ["btnStreamSend", "btnStreamSend"], ["btnStreamBack", "btnStreamBack"],
    ["streamConnTitle", "streamConnTitle"], ["streamUrlHint", "streamUrlHint"], ["streamTokenHint", "streamTokenHint"],
  ];
  for (const [id, key] of map) {
    const el = document.getElementById(id);
    if (el) el.textContent = t(key);
  }
  const steps = document.getElementById("streamSteps");
  if (steps) {
    steps.innerHTML = [t("streamStep1"), t("streamStep2"), t("streamStep3")]
      .map((s) => `<li>${esc(s)}</li>`).join("");
  }
  setStreamStatusTag("idle");
  // Switch aria labels (title is visible; keep accessible name on the control)
  const switchLabels = [
    ["schedEnabled", "lblSchedEnabled"],
    ["schedTelegram", "lblSchedTelegram"],
    ["tgEnabled", "lblTgEnabled"],
    ["proxyEnabled", "lblProxyEnabled"],
  ];
  for (const [id, key] of switchLabels) {
    const el = document.getElementById(id);
    if (el) el.setAttribute("aria-label", t(key));
  }
  document.getElementById("authTitle").textContent = setupMode ? t("authSetupTitle") : t("authLoginTitle");
  document.getElementById("authDesc").textContent = setupMode ? t("authSetupDesc") : t("authLoginDesc");
  document.getElementById("btnAuthSubmit").textContent = setupMode ? t("btnSetup") : t("btnLogin");
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) data = await res.json();
  else data = await res.text();
  if (!res.ok) {
    const detail = (data && data.detail) || data || res.statusText;
    let msg = typeof detail === "string" ? detail : JSON.stringify(detail);
    if (msg === "setup_token_invalid") msg = t("errSetupToken");
    else if (msg === "password_mismatch") msg = t("errPassMismatch");
    else if (msg === "password_already_set") msg = t("errPassSet");
    else if (String(msg).startsWith("weak_password")) msg = t("errWeakPass");
    else if (msg === "invalid_password") msg = t("errBadPass");
    else if (msg === "too_many_attempts") msg = t("errThrottle");
    else if (msg === "Internal Server Error") msg = t("errServer");
    throw new Error(msg);
  }
  return data;
}

function showPanel(id) {
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  document.getElementById(id)?.classList.add("active");
}

function showBackupTab(tab) {
  document.getElementById("backupTabs").classList.remove("hidden");
  document.getElementById("btnLogout").classList.remove("hidden");
  document.querySelectorAll("#backupTabs .backup-tab").forEach((s) => {
    s.classList.toggle("active", s.dataset.tab === tab);
  });
  if (tab === "dash") {
    showPanel("panel-dash");
    refreshDashboard();
  } else if (tab === "list") {
    showPanel("panel-list");
    refreshList();
  } else if (tab === "settings") {
    showPanel("panel-settings");
    loadSettingsForm();
  } else if (tab === "stream") {
    showPanel("panel-stream");
  }
}

function humanSize(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return (i === 0 ? String(Math.round(v)) : v.toFixed(1)) + " " + units[i];
}

function metricCard({ tone, icon, value, label, sub }) {
  return `<div class="backup-metric">
    <div class="backup-metric-top">
      <span class="choice-icon ${tone}" aria-hidden="true">${icon}</span>
    </div>
    <div>
      <strong>${esc(value)}</strong>
      <div class="metric-label">${esc(label)}</div>
      ${sub ? `<div class="metric-sub">${esc(sub)}</div>` : ""}
    </div>
  </div>`;
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function boot() {
  setBackupLang(lang);
  const st = await api("/api/setup/status");
  document.getElementById("appVersion").textContent = "v" + (st.version || "4.0.1");
  setupMode = !st.password_set;
  window.__setupTokenRequired = !!st.setup_token_required;
  document.getElementById("authConfirmWrap").classList.toggle("hidden", !setupMode);
  document.getElementById("authSetupTokenWrap").classList.toggle(
    "hidden",
    !(setupMode && window.__setupTokenRequired)
  );
  applyI18n();

  if (!setupMode) {
    try {
      await api("/api/dashboard");
      enterApp();
      return;
    } catch (_) { /* need login */ }
  }
  showPanel("panel-auth");
}

function enterApp() {
  showBackupTab("dash");
}

async function submitAuth() {
  const err = document.getElementById("authError");
  err.classList.add("hidden");
  const password = document.getElementById("authPassword").value;
  try {
    if (setupMode) {
      const password_confirm = document.getElementById("authPasswordConfirm").value;
      const body = { password, password_confirm };
      if (window.__setupTokenRequired) {
        body.setup_token = document.getElementById("authSetupToken").value.trim();
      }
      await api("/api/setup/password", {
        method: "POST",
        body: JSON.stringify(body),
      });
    } else {
      await api("/api/login", { method: "POST", body: JSON.stringify({ password }) });
    }
    enterApp();
  } catch (e) {
    err.textContent = e.message || String(e);
    err.classList.remove("hidden");
  }
}

async function logoutBackup() {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch (_) {}
  location.href = "/login";
}

async function refreshDashboard() {
  const data = await api("/api/dashboard");
  const sys = data.system || {};
  const health = data.health || {};
  const access = data.panel_access || {};
  const tg = data.telegram || {};
  const sched = data.schedule || {};
  const installed = data.pasarguard_installed;

  document.getElementById("dashPgCard").className = installed
    ? "success-card backup-section-card"
    : "warning-card backup-section-card";
  document.getElementById("dashPgSub").textContent = installed ? t("dashPgSubOk") : t("dashPgSubMissing");

  const specs = document.getElementById("dashPgSpecs");
  specs.innerHTML = [
    ["PasarGuard", installed ? "OK" : "—"],
    [t("db"), access.db_type || sys.pasarguard_db || "—"],
    [t("port"), access.port || "—"],
    [t("ssl"), access.ssl == null ? "—" : (access.ssl ? t("yes") : t("no"))],
  ].map(([label, value]) => `
    <div class="specs-item">
      <span class="specs-label">${esc(label)}</span>
      <span class="specs-value" title="${esc(value)}">${esc(value)}</span>
    </div>
  `).join("");

  const diskFree = health.backup_disk_free_bytes;
  const diskTotal = health.backup_disk_total_bytes;
  const memFree = health.memory_available_bytes;
  const load = health.load_ratio_1m;
  document.getElementById("dashHealthGrid").innerHTML = [
    metricCard({
      tone: "icon-tone-blue",
      icon: ICONS.shield,
      value: installed ? "OK" : "—",
      label: t("healthPg"),
      sub: access.db_type || sys.pasarguard_db || "—",
    }),
    metricCard({
      tone: "icon-tone-cyan",
      icon: ICONS.docker,
      value: sys.docker ? "OK" : "—",
      label: t("healthDocker"),
      sub: `${t("profile")}: ${health.profile || "—"}`,
    }),
    metricCard({
      tone: "icon-tone-orange",
      icon: ICONS.disk,
      value: humanSize(diskFree),
      label: t("healthDisk"),
      sub: diskTotal != null ? `${t("freeOf")} ${humanSize(diskTotal)}` : "",
    }),
    metricCard({
      tone: "icon-tone-green",
      icon: ICONS.cpu,
      value: memFree != null ? humanSize(memFree) : "—",
      label: t("healthMem"),
      sub: health.cpu_count != null ? `CPU ${health.cpu_count} · load ${load ?? "—"}` : "",
    }),
    metricCard({
      tone: "icon-tone-yellow",
      icon: ICONS.archive,
      value: String(data.backup_count ?? 0),
      label: t("healthArchives"),
      sub: `${t("totalSize")}: ${humanSize(data.backup_total_bytes)}`,
    }),
    metricCard({
      tone: "icon-tone-blue",
      icon: ICONS.clock,
      value: sched.enabled ? t("scheduleOn") : t("scheduleOff"),
      label: t("keepLast"),
      sub: `${data.retention_count || 10} · ${String(sched.hour ?? 3).padStart(2, "0")}:${String(sched.minute ?? 0).padStart(2, "0")} UTC`,
    }),
  ].join("");

  const last = data.last_backup;
  const lastBox = document.getElementById("dashLast");
  if (!last) {
    lastBox.classList.remove("is-detailed");
    lastBox.innerHTML = `<div class="backup-section-head">
      <span class="choice-icon icon-tone-yellow" aria-hidden="true">${ICONS.archive}</span>
      <div><h3 style="margin:0;font-size:1rem;line-height:1.35">${esc(t("lastBackup"))}</h3>
      <p class="desc-sm" style="margin:2px 0 0">${esc(t("noLast"))}</p></div></div>`;
  } else {
    const c = last.counts || {};
    lastBox.classList.add("is-detailed");
    lastBox.innerHTML = `<div class="backup-section-head">
      <span class="choice-icon icon-tone-green" aria-hidden="true">${ICONS.archive}</span>
      <div><h3 style="margin:0;font-size:1rem;line-height:1.35">${esc(t("lastBackup"))}</h3>
      <p class="desc-sm" style="margin:2px 0 0;word-break:break-all">${esc(last.filename || last.backup_id)}</p></div></div>
      <div class="specs-grid">
        <div class="specs-item"><span class="specs-label">${esc(t("size"))}</span><span class="specs-value">${esc(humanSize(last.size_bytes))}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("db"))}</span><span class="specs-value">${esc(last.db_type || "—")}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("users"))}</span><span class="specs-value">${esc(c.users ?? "—")}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("nodes"))}</span><span class="specs-value">${esc(c.nodes ?? "—")}</span></div>
        <div class="specs-item"><span class="specs-label">UTC</span><span class="specs-value">${esc(last.created_at || "—")}</span></div>
      </div>`;
  }

  const delivery = document.getElementById("dashDelivery");
  delivery.classList.add("is-detailed");
  let tgLive = { connected: false };
  try {
    tgLive = await api("/api/telegram/status");
  } catch (_) {
    tgLive = { connected: false };
  }
  const tgTagClass = tgLive.connected ? "is-connected" : "is-disconnected";
  const tgTagLabel = tgLive.connected ? t("tgConnected") : t("tgDisconnected");
  delivery.innerHTML = `<div class="backup-section-head">
      <span class="choice-icon icon-tone-cyan" aria-hidden="true">${ICONS.send}</span>
      <div><h3 style="margin:0;font-size:1rem;line-height:1.35">${esc(t("deliveryTitle"))} <span class="backup-status-tag ${tgTagClass}">${esc(tgTagLabel)}</span></h3>
      <p class="desc-sm" style="margin:2px 0 0">${esc(tg.enabled ? t("telegramOn") : t("telegramOff"))} · ${esc(tg.configured ? t("telegramReady") : t("telegramNeedConfig"))}</p></div></div>
      <div class="backup-item-chips">
        <span class="backup-chip">${esc(sched.enabled ? t("scheduleOn") : t("scheduleOff"))}</span>
        <span class="backup-chip">${esc(tg.proxy_enabled ? t("proxyOn") : "Proxy —")}</span>
        <span class="backup-chip">${esc(data.stream_dest ? t("streamSet") : t("streamUnset"))}</span>
      </div>
      ${data.stream_dest ? `<p class="desc-sm" style="margin-top:10px;word-break:break-all">${esc(data.stream_dest)}</p>` : ""}`;

  setTelegramStatusTag({ connected: !!tgLive.connected });

  const errBox = document.getElementById("dashError");
  if (data.last_error && data.last_error.message) {
    errBox.classList.remove("hidden");
    errBox.innerHTML = `<strong>${esc(t("lastError"))}</strong><br>${esc(data.last_error.at || "")} · ${esc(data.last_error.message)}`;
  } else {
    errBox.classList.add("hidden");
    errBox.textContent = "";
  }
}

async function createBackupNow() {
  const box = document.getElementById("backupProgress");
  const title = document.getElementById("backupProgressTitle");
  const logEl = document.getElementById("backupProgressLog");
  box.classList.remove("hidden", "is-success", "is-error");
  box.classList.add("is-running");
  title.textContent = t("backupRunning");
  logEl.textContent = "";
  document.getElementById("btnBackupNow").disabled = true;
  const listBtn = document.getElementById("btnBackupNowList");
  if (listBtn) listBtn.disabled = true;
  try {
    const job = await api("/api/backups/create", { method: "POST", body: "{}" });
    await pollJob(job.job_id, title, logEl, box);
    box.classList.remove("is-running");
    box.classList.add("is-success");
    await refreshDashboard();
    await refreshList();
  } catch (e) {
    box.classList.remove("is-running");
    box.classList.add("is-error");
    title.textContent = t("backupFail") + ": " + e.message;
  } finally {
    document.getElementById("btnBackupNow").disabled = false;
    if (listBtn) listBtn.disabled = false;
  }
}

async function pollJob(jobId, title, logEl, box) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const job = await api("/api/backups/jobs/" + jobId);
        logEl.textContent = (job.logs || []).join("\n");
        logEl.scrollTop = logEl.scrollHeight;
        if (job.status === "success") {
          title.textContent = t("backupDone") + (job.filename ? ": " + job.filename : "");
          resolve(job);
          return;
        }
        if (job.status === "error") {
          if (box) {
            box.classList.remove("is-running");
            box.classList.add("is-error");
          }
          title.textContent = t("backupFail") + ": " + (job.error || "");
          reject(new Error(job.error || "error"));
          return;
        }
        pollTimer = setTimeout(tick, 1000);
      } catch (e) {
        reject(e);
      }
    };
    tick();
  });
}

async function refreshList() {
  const data = await api("/api/backups");
  const root = document.getElementById("backupList");
  const items = data.items || [];
  if (!items.length) {
    root.innerHTML = `<div class="info-box backup-empty">
      <span class="choice-icon icon-tone-yellow" aria-hidden="true">${ICONS.empty}</span>
      <p>${t("emptyList")}</p>
    </div>`;
    return;
  }
  root.innerHTML = items.map((it) => {
    const m = it.manifest || {};
    const c = m.counts || {};
    return `<div class="backup-item">
      <div class="backup-item-head">
        <div style="display:flex;gap:12px;align-items:flex-start;min-width:0">
          <span class="choice-icon icon-tone-blue" aria-hidden="true">${ICONS.archive}</span>
          <div style="min-width:0">
            <strong>${it.filename}</strong>
            <div class="backup-item-meta">${it.mtime || ""} · ${m.db_type || "?"}</div>
          </div>
        </div>
        <span class="backup-item-badge">${humanSize(it.size_bytes)}</span>
      </div>
      <div class="backup-item-chips">
        <span class="backup-chip">${t("users")}: ${c.users ?? "—"}</span>
        <span class="backup-chip">${t("nodes")}: ${c.nodes ?? "—"}</span>
        <span class="backup-chip">${t("admins")}: ${c.admins ?? "—"}</span>
        <span class="backup-chip">${t("inbounds")}: ${c.inbounds ?? "—"}</span>
      </div>
      <div class="backup-item-actions">
        <a class="btn btn-primary btn-sm" href="/api/backups/${encodeURIComponent(it.id)}/download">${t("download")}</a>
        <button type="button" class="btn btn-back btn-sm" onclick="sendTelegram('${it.id}')">${t("sendTg")}</button>
        <button type="button" class="btn btn-back btn-sm" onclick="openStream('${it.id}')">${t("sendStream")}</button>
        <button type="button" class="btn btn-back btn-sm" onclick="deleteBackup('${it.id}')">${t("remove")}</button>
      </div>
    </div>`;
  }).join("");
}

async function sendTelegram(id) {
  try {
    const r = await api("/api/backups/" + encodeURIComponent(id) + "/telegram", { method: "POST", body: "{}" });
    alert(r.ok ? `OK · parts=${r.parts}` : (r.error || "fail"));
  } catch (e) {
    alert(e.message);
  }
}

async function deleteBackup(id) {
  if (!confirm(t("confirmDelete"))) return;
  await api("/api/backups/" + encodeURIComponent(id), { method: "DELETE" });
  refreshList();
  refreshDashboard();
}

async function openStream(id) {
  document.getElementById("streamBackupId").value = id;
  const settings = await api("/api/settings");
  document.getElementById("streamUrl").value = (settings.stream && settings.stream.default_dest_url) || "";
  document.getElementById("streamToken").value = "";
  document.getElementById("streamMsg").classList.add("hidden");
  document.getElementById("streamProgress").classList.add("hidden");
  setStreamStatusTag("idle");
  showBackupTab("stream");
}

function setStreamStatusTag(state) {
  const el = document.getElementById("streamStatusTag");
  if (!el) return;
  el.classList.remove("is-connected", "is-disconnected", "is-unknown");
  const map = {
    idle: ["is-unknown", "streamIdle"],
    connecting: ["is-unknown", "streamConnecting"],
    sending: ["is-unknown", "streamSending"],
    success: ["is-connected", "streamSuccess"],
    error: ["is-disconnected", "streamFail"],
  };
  const [cls, key] = map[state] || map.idle;
  el.classList.add(cls);
  el.textContent = t(key);
}

function setStreamProgressUI({ title, pct, meta, running }) {
  const box = document.getElementById("streamProgress");
  const bar = document.getElementById("streamProgressBar");
  const titleEl = document.getElementById("streamProgressTitle");
  const metaEl = document.getElementById("streamProgressMeta");
  box.classList.remove("hidden", "is-running", "is-success", "is-error");
  if (running) box.classList.add("is-running");
  titleEl.textContent = title || "";
  metaEl.textContent = meta || "";
  const width = Math.max(0, Math.min(100, Number(pct) || 0));
  bar.style.width = width + "%";
  bar.style.animation = running && width < 100 ? "" : "none";
}

async function sendStream() {
  const msg = document.getElementById("streamMsg");
  const btn = document.getElementById("btnStreamSend");
  msg.classList.add("hidden");
  msg.textContent = "";
  setStreamStatusTag("connecting");
  setStreamProgressUI({ title: t("streamConnecting"), pct: 0, meta: "", running: true });
  if (btn) btn.disabled = true;
  try {
    const started = await api("/api/backups/stream/send", {
      method: "POST",
      body: JSON.stringify({
        backup_id: document.getElementById("streamBackupId").value,
        dest_url: document.getElementById("streamUrl").value,
        token: document.getElementById("streamToken").value,
      }),
    });
    const jobId = started.job_id;
    if (!jobId) throw new Error("no job_id");
    await pollStreamJob(jobId);
  } catch (e) {
    setStreamStatusTag("error");
    setStreamProgressUI({ title: t("streamFail") + ": " + e.message, pct: 100, meta: "", running: false });
    const box = document.getElementById("streamProgress");
    box.classList.add("is-error");
    msg.classList.remove("hidden");
    msg.textContent = e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function pollStreamJob(jobId) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const job = await api("/api/backups/stream/jobs/" + encodeURIComponent(jobId));
        const total = Number(job.bytes_total) || 0;
        const sent = Number(job.bytes_sent) || 0;
        const pct = total > 0 ? Math.round((sent / total) * 100) : (job.status === "success" ? 100 : 5);
        const meta = total > 0
          ? `${humanSize(sent)} / ${humanSize(total)} (${pct}%)`
          : humanSize(sent);
        if (job.status === "connecting" || job.status === "queued") {
          setStreamStatusTag("connecting");
          setStreamProgressUI({ title: t("streamConnecting"), pct: Math.max(pct, 2), meta, running: true });
        } else if (job.status === "sending") {
          setStreamStatusTag("sending");
          setStreamProgressUI({ title: t("streamSending"), pct, meta, running: true });
        } else if (job.status === "success") {
          setStreamStatusTag("success");
          setStreamProgressUI({ title: t("streamSuccess"), pct: 100, meta, running: false });
          document.getElementById("streamProgress").classList.add("is-success");
          const msg = document.getElementById("streamMsg");
          msg.classList.remove("hidden");
          msg.textContent = t("streamSuccess") + (job.result && job.result.sha256 ? " · sha256=" + job.result.sha256.slice(0, 12) + "…" : "");
          resolve(job);
          return;
        } else if (job.status === "error") {
          reject(new Error(job.error || "stream_failed"));
          return;
        }
        setTimeout(tick, 700);
      } catch (e) {
        reject(e);
      }
    };
    tick();
  });
}

async function loadSettingsForm() {
  const s = await api("/api/settings");
  const sched = s.schedule || {};
  const tg = s.telegram || {};
  document.getElementById("schedEnabled").checked = !!sched.enabled;
  document.getElementById("schedHour").value = sched.hour ?? 3;
  document.getElementById("schedMinute").value = sched.minute ?? 0;
  document.getElementById("retentionCount").value = s.retention_count ?? 10;
  document.getElementById("schedTelegram").checked = !!sched.send_telegram;
  document.getElementById("tgEnabled").checked = !!tg.enabled;
  document.getElementById("tgToken").value = "";
  document.getElementById("tgToken").placeholder = tg.bot_token_hint || "";
  document.getElementById("tgTokenHint").textContent = tg.bot_token_set ? (tg.bot_token_hint || "••••") : "";
  document.getElementById("tgChat").value = tg.chat_id || "";
  document.getElementById("tgCaption").value = tg.caption_template || "";
  document.getElementById("proxyEnabled").checked = !!tg.proxy_enabled;
  document.getElementById("proxyType").value = tg.proxy_type || "socks5";
  document.getElementById("proxyHost").value = tg.proxy_host || "";
  document.getElementById("proxyPort").value = tg.proxy_port || 1080;
  document.getElementById("proxyUser").value = tg.proxy_user || "";
  document.getElementById("proxyPass").value = "";
  document.getElementById("streamDest").value = (s.stream && s.stream.default_dest_url) || "";
  setTelegramStatusTag({ checking: true });
  refreshTelegramStatusTag().catch(() => setTelegramStatusTag({ connected: false }));
}

function setTelegramStatusTag({ connected, checking } = {}) {
  const el = document.getElementById("tgStatusTag");
  if (!el) return;
  el.classList.remove("is-connected", "is-disconnected", "is-unknown");
  if (checking) {
    el.classList.add("is-unknown");
    el.textContent = t("tgChecking");
    return;
  }
  if (connected) {
    el.classList.add("is-connected");
    el.textContent = t("tgConnected");
  } else {
    el.classList.add("is-disconnected");
    el.textContent = t("tgDisconnected");
  }
}

async function refreshTelegramStatusTag() {
  setTelegramStatusTag({ checking: true });
  try {
    const st = await api("/api/telegram/status");
    setTelegramStatusTag({ connected: !!st.connected });
    return st;
  } catch (_) {
    setTelegramStatusTag({ connected: false });
    return null;
  }
}

async function saveSettings() {
  const msg = document.getElementById("settingsMsg");
  msg.classList.add("hidden");
  const tokenVal = document.getElementById("tgToken").value.trim();
  const patch = {
    retention_count: Number(document.getElementById("retentionCount").value || 10),
    schedule: {
      enabled: document.getElementById("schedEnabled").checked,
      hour: Number(document.getElementById("schedHour").value || 0),
      minute: Number(document.getElementById("schedMinute").value || 0),
      send_telegram: document.getElementById("schedTelegram").checked,
    },
    telegram: {
      enabled: document.getElementById("tgEnabled").checked,
      bot_token: tokenVal,
      chat_id: document.getElementById("tgChat").value.trim(),
      caption_template: document.getElementById("tgCaption").value,
      proxy_enabled: document.getElementById("proxyEnabled").checked,
      proxy_type: document.getElementById("proxyType").value,
      proxy_host: document.getElementById("proxyHost").value.trim(),
      proxy_port: Number(document.getElementById("proxyPort").value || 1080),
      proxy_user: document.getElementById("proxyUser").value.trim(),
      proxy_password: document.getElementById("proxyPass").value,
    },
    stream: {
      default_dest_url: document.getElementById("streamDest").value.trim(),
    },
  };
  await api("/api/settings", { method: "PUT", body: JSON.stringify(patch) });

  const np = document.getElementById("newPassword").value;
  const npc = document.getElementById("newPasswordConfirm").value;
  const cur = document.getElementById("currentPassword").value;
  if (np || npc || cur) {
    await api("/api/password/change", {
      method: "POST",
      body: JSON.stringify({
        current_password: cur,
        password: np,
        password_confirm: npc,
      }),
    });
    document.getElementById("currentPassword").value = "";
    document.getElementById("newPassword").value = "";
    document.getElementById("newPasswordConfirm").value = "";
  }
  msg.textContent = t("saved");
  msg.classList.remove("hidden");
  refreshTelegramStatusTag().catch(() => {});
}

async function testTelegram() {
  try {
    await saveSettings();
    const r = await api("/api/telegram/test", { method: "POST", body: "{}" });
    setTelegramStatusTag({ connected: true });
    alert("OK · @" + ((r.bot && r.bot.username) || "?"));
  } catch (e) {
    setTelegramStatusTag({ connected: false });
    alert(e.message);
  }
}

async function previewTelegram() {
  const box = document.getElementById("tgPreviewBox");
  const r = await api("/api/telegram/preview", {
    method: "POST",
    body: JSON.stringify({ caption_template: document.getElementById("tgCaption").value }),
  });
  box.textContent = r.text || "";
  box.classList.remove("hidden");
}

boot().catch((e) => {
  const err = document.getElementById("authError");
  err.textContent = e.message || String(e);
  err.classList.remove("hidden");
  showPanel("panel-auth");
});
