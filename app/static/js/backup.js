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
  stream: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><rect x="2" y="4" width="6" height="16" rx="1.5"/><rect x="16" y="4" width="6" height="16" rx="1.5"/><path d="M10 12h4"/><path d="M12 9.5L14.5 12 12 14.5"/></svg>',
  download: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 19h14"/></svg>',
  trash: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M4 7h16"/><path d="M9 7V5h6v2"/><path d="M7 7l1 12h8l1-12"/></svg>',
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
    authSetupDesc: "اولین ورود است؛ یک رمز قوی برای این پنل تنظیم کنید.",
    lblSetupToken: "توکن نصب (یک‌بارمصرف)",
    authSetupTokenHint: "همان توکنی که در پایان نصب چاپ شد را اینجا بگذارید.",
    lblCurrentPass: "رمز فعلی",
    errSetupToken: "توکن نصب اشتباه است. توکن چاپ‌شده را بدون فاصله کپی کنید.",
    errPassMismatch: "رمز و تکرار آن یکی نیست.",
    errPassSet: "رمز قبلاً تنظیم شده؛ از ورود استفاده کنید.",
    errWeakPass: "رمز ضعیف است. حداقل ۱۲ کاراکتر با حرف بزرگ، کوچک، عدد و نماد بگذارید.",
    errBadPass: "رمز اشتباه است.",
    errThrottle: "تلاش‌های زیاد — چند دقیقه صبر کنید و دوباره امتحان کنید.",
    errServer: "خطای سرور. لاگ را ببینید: journalctl -u pg-backup -n 50",
    authLoginTitle: "ورود به پنل بکاپ",
    authLoginDesc: "با رمز همین پنل وارد شوید.",
    lblPassword: "رمز عبور",
    lblPasswordConfirm: "تکرار رمز",
    authPolicy: "حداقل ۱۲ کاراکتر؛ شامل حرف بزرگ، کوچک، عدد و نماد.",
    btnSetup: "ذخیره و ورود",
    btnLogin: "ورود",
    dashH2: "پایش",
    dashDesc: "وضعیت سرور، پنل و آخرین بکاپ را یکجا ببینید.",
    dashPgTitle: "وضعیت PasarGuard",
    dashPgSubOk: "پنل روی این سرور نصب است و آماده بکاپ‌گیری است.",
    dashPgSubMissing: "PasarGuard پیدا نشد. اول پنل را روی سرور نصب کنید.",
    btnOpenPanel: "ورود به پنل PasarGuard",
    updateBannerTitle: "نسخه جدید آماده است",
    updateBannerSub: "نسخه {latest} منتشر شده. از تنظیمات می‌توانید به‌روزرسانی کنید.",
    btnUpdateGo: "رفتن به آپدیت",
    setUpdateTitle: "به‌روزرسانی پنل بکاپ",
    setUpdateHint: "از GitHub نسخه جدید را بررسی و روی همین سرور نصب کنید. تنظیمات و بکاپ‌ها حفظ می‌شوند.",
    lblUpdateCurrent: "نسخه فعلی",
    lblUpdateLatest: "آخرین نسخه",
    lblUpdateChangelog: "تغییرات این نسخه",
    btnUpdateCheck: "بررسی آپدیت",
    btnUpdateApply: "نصب به‌روزرسانی",
    updateAvailable: "نسخه جدید در دسترس است.",
    updateUpToDate: "پنل به‌روز است.",
    updateChecking: "در حال بررسی…",
    updateApplying: "در حال نصب به‌روزرسانی…",
    updateDone: "به‌روزرسانی نصب شد. سرویس در حال راه‌اندازی دوباره است.",
    updateFail: "به‌روزرسانی انجام نشد",
    updateCheckFail: "بررسی آپدیت انجام نشد",
    errorCleared: "خطا پاک شد",
    secHealthTitle: "سلامت سرور و بکاپ",
    secPgTitle: "جزئیات پنل",
    secStatsTitle: "آمار زنده پنل",
    secBackupStatusTitle: "وضعیت بکاپ و ارسال",
    btnBackupNow: "بکاپ کامل همین حالا",
    backupRunning: "در حال ساخت بکاپ…",
    backupDone: "بکاپ آماده شد",
    backupFail: "بکاپ انجام نشد",
    listH2: "فایل‌های بکاپ",
    listDesc: "دانلود کنید، به تلگرام بفرستید، یا به سرور مقصد استریم کنید.",
    emptyList: "هنوز بکاپی ندارید. با دکمه بالا یک بکاپ کامل بگیرید.",
    download: "دانلود",
    sendTg: "تلگرام",
    sendStream: "استریم",
    remove: "حذف",
    latestBackupTag: "آخرین بکاپ",
    backupsPath: "مسیر ذخیره",
    modalOk: "باشه",
    modalCancel: "انصراف",
    modalConfirm: "تأیید",
    modalNotice: "پیام",
    confirmDeleteTitle: "حذف بکاپ",
    tgTestOkTitle: "تست تلگرام",
    tgTestFailTitle: "خطای تلگرام",
    tgTestOkBody: "اتصال برقرار شد و فایل بکاپ به تلگرام ارسال شد",
    tgSendOk: "فایل بکاپ به تلگرام ارسال شد",
    tgSendFail: "ارسال به تلگرام انجام نشد",
    tgAutoSent: "بکاپ به تلگرام هم ارسال شد",
    tgAutoFail: "بکاپ ساخته شد، ولی ارسال تلگرام انجام نشد",
    streamFailTitle: "خطای استریم",
    clearErrorFailTitle: "خطا",
    confirmDelete: "این بکاپ حذف شود؟",
    setH2: "تنظیمات",
    setDesc: "هر بخش جداست؛ فقط همان چیزی که لازم دارید را تغییر دهید.",
    setSchedTitle: "زمان‌بندی خودکار",
    setSchedHint: "بازه و منطقهٔ زمانی بکاپ خودکار را تنظیم کنید.",
    lblSchedEnabled: "بکاپ خودکار فعال باشد",
    lblSchedInterval: "بازهٔ زمانی",
    lblSchedTimezone: "منطقهٔ زمانی",
    schedInterval1: "هر ۱ ساعت",
    schedInterval3: "هر ۳ ساعت",
    schedInterval6: "هر ۶ ساعت",
    schedInterval12: "هر ۱۲ ساعت",
    schedInterval24: "هر ۲۴ ساعت",
    lblRetention: "تعداد نگه‌داری",
    lblRetentionDays: "حداکثر سن (روز)",
    retentionDaysHint: "۰ = فقط بر اساس تعداد. مثلاً ۷ یعنی فایل‌های قدیمی‌تر از ۷ روز حذف شوند.",
    lblSchedTelegram: "بعد از بکاپ خودکار، به تلگرام هم بفرست",
    lblSchedNotifyFail: "اعلان شکست زمان‌بندی (تلگرام)",
    lblIntegrityVerify: "تأیید سلامت بکاپ بعد از ساخت (SHA256 + CRC)",
    schedNextHintEmpty: "با روشن کردن زمان‌بندی، زمان اجرای بعدی اینجا دیده می‌شود.",
    schedNextHint: "اجرای بعدی: {time}",
    setTgTitle: "تلگرام",
    setTgHint: "اختیاری است. فایل روی سرور یک‌تکه می‌ماند و فقط هنگام ارسال به تلگرام در صورت نیاز تکه‌تکه می‌شود.",
    lblTgEnabled: "ارسال به تلگرام فعال باشد",
    lblTgToken: "Bot Token",
    lblTgChat: "Admin ID / Chat ID",
    lblTgTopic: "Topic ID (اختیاری)",
    tgTopicHint: "برای گروه فوروم تلگرام؛ خالی بگذارید اگر تاپیک ندارید.",
    lblTgChat2: "مقصد دوم (Chat ID)",
    lblTgTopic2: "Topic مقصد دوم",
    tgDestHint: "می‌توانید یک چت/تاپیک دوم برای بکاپ بگذارید؛ هر دو مقصد فایل را می‌گیرند.",
    lblTgCaption: "متن پیام",
    tgCaptionHint: "متغیرها: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgTest: "اتصال و ارسال بکاپ",
    setWebhookTitle: "وب‌هوک اعلان شکست",
    setWebhookHint: "در صورت شکست بکاپ زمان‌بندی‌شده، یک JSON به این آدرس POST می‌شود.",
    lblWebhookUrl: "آدرس وب‌هوک (HTTPS/HTTP عمومی)",
    lblWebhookEnabled: "وب‌هوک فعال باشد",
    setProxyTitle: "پروکسی تلگرام",
    setProxyHint: "اگر تلگرام مستقیم در دسترس نیست، پروکسی بگذارید.",
    lblProxyEnabled: "از پروکسی استفاده کن",
    lblProxyType: "نوع",
    lblProxyHost: "هاست",
    lblProxyPort: "پورت",
    lblProxyUser: "کاربر",
    lblProxyPass: "رمز پروکسی",
    setPassTitle: "تغییر رمز پنل",
    setPassHint: "اگر نمی‌خواهید رمز عوض شود، این بخش را خالی بگذارید.",
    lblNewPass: "رمز جدید",
    lblNewPassConfirm: "تکرار رمز جدید",
    btnSaveSettings: "ذخیره تنظیمات",
    saved: "ذخیره شد",
    streamH2: "ارسال استریم به ویزارد",
    streamDesc: "فایل بکاپ روی همین سرور می‌ماند؛ فقط یک کپی به ویزارد مقصد فرستاده می‌شود. اول مقصد را در حالت دریافت بگذارید، بعد از اینجا ارسال کنید.",
    streamStep1: "روی سرور مقصد: ویزارد → ریستور → آماده‌سازی دریافت استریم",
    streamStep2: "توکن یک‌بارمصرف را کپی کنید (حدود ۳۰ دقیقه اعتبار دارد)",
    streamStep3: "اینجا آدرس ویزارد مقصد و توکن را وارد کنید و ارسال را بزنید",
    streamConnTitle: "وضعیت ارسال",
    streamUrlHint: "مثال: http://203.0.113.10:7000 — همان پورتی که ویزارد مقصد روی آن گوش می‌دهد.",
    streamTokenHint: "توکنی که بعد از «آماده‌سازی دریافت استریم» روی ویزارد مقصد نمایش داده می‌شود.",
    streamIdle: "آماده",
    streamConnecting: "در حال اتصال به مقصد…",
    streamSending: "در حال ارسال…",
    streamSuccess: "ارسال کامل شد",
    streamFail: "ارسال انجام نشد",
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
    time: "زمان",
    healthPg: "PasarGuard",
    healthDocker: "Docker",
    healthDisk: "فضای بکاپ",
    healthMem: "رم آزاد",
    healthCpu: "CPU / Load",
    healthArchives: "آرشیو بکاپ",
    healthSchedule: "زمان‌بندی",
    healthOk: "آماده",
    healthMissing: "یافت نشد",
    healthOff: "خاموش",
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
    panelUrl: "آدرس پنل",
    ssl: "SSL",
    port: "پورت",
    yes: "بله",
    no: "خیر",
    lastError: "آخرین خطا",
    clearError: "حذف",
    deliveryTitle: "ارسال و زمان‌بندی",
    profile: "پروفایل منابع",
  },
  en: {
    logout: "Log out",
    tabDash: "Dashboard",
    tabList: "Backups",
    tabSettings: "Settings",
    authSetupTitle: "Backup panel password",
    authSetupDesc: "First login: choose a strong password for this panel.",
    lblSetupToken: "Setup token (one-time)",
    authSetupTokenHint: "Paste the token printed at the end of installation.",
    lblCurrentPass: "Current password",
    errSetupToken: "Invalid setup token. Copy the installer token exactly, with no spaces.",
    errPassMismatch: "Password and confirmation do not match.",
    errPassSet: "Password is already set — use login instead.",
    errWeakPass: "Weak password. Use at least 12 characters with upper, lower, digit, and symbol.",
    errBadPass: "Wrong password.",
    errThrottle: "Too many attempts — wait a few minutes and try again.",
    errServer: "Server error. Check logs: journalctl -u pg-backup -n 50",
    authLoginTitle: "Backup panel login",
    authLoginDesc: "Sign in with this panel’s password.",
    lblPassword: "Password",
    lblPasswordConfirm: "Confirm password",
    authPolicy: "At least 12 characters, including upper, lower, digit, and symbol.",
    btnSetup: "Save & enter",
    btnLogin: "Sign in",
    dashH2: "Overview",
    dashDesc: "See server health, panel status, and the latest backup in one place.",
    dashPgTitle: "PasarGuard status",
    dashPgSubOk: "The panel is installed on this server and ready for backups.",
    dashPgSubMissing: "PasarGuard was not found. Install the panel on this server first.",
    btnOpenPanel: "Open PasarGuard panel",
    updateBannerTitle: "Update available",
    updateBannerSub: "Version {latest} is available. You can update from Settings.",
    btnUpdateGo: "Go to update",
    setUpdateTitle: "Backup panel updates",
    setUpdateHint: "Check GitHub for a newer release and install it on this server. Settings and backups are kept.",
    lblUpdateCurrent: "Current version",
    lblUpdateLatest: "Latest version",
    lblUpdateChangelog: "What’s new",
    btnUpdateCheck: "Check for updates",
    btnUpdateApply: "Install update",
    updateAvailable: "A newer version is available.",
    updateUpToDate: "You’re up to date.",
    updateChecking: "Checking…",
    updateApplying: "Installing update…",
    updateDone: "Update installed. The service is restarting.",
    updateFail: "Update failed",
    updateCheckFail: "Could not check for updates",
    errorCleared: "Error cleared",
    secHealthTitle: "Server & backup health",
    secPgTitle: "Panel details",
    secStatsTitle: "Live panel stats",
    secBackupStatusTitle: "Backup & delivery",
    btnBackupNow: "Create full backup now",
    backupRunning: "Creating backup…",
    backupDone: "Backup ready",
    backupFail: "Backup failed",
    listH2: "Backup files",
    listDesc: "Download a file, send it to Telegram, or stream it to another server.",
    emptyList: "No backups yet. Create a full backup with the button above.",
    download: "Download",
    sendTg: "Telegram",
    sendStream: "Stream",
    remove: "Delete",
    latestBackupTag: "Latest",
    backupsPath: "Storage path",
    modalOk: "OK",
    modalCancel: "Cancel",
    modalConfirm: "Confirm",
    modalNotice: "Notice",
    confirmDeleteTitle: "Delete backup",
    tgTestOkTitle: "Telegram test",
    tgTestFailTitle: "Telegram error",
    tgTestOkBody: "Connected — the backup file was sent to Telegram",
    tgSendOk: "Backup file sent to Telegram",
    tgSendFail: "Could not send to Telegram",
    tgAutoSent: "Backup was also sent to Telegram",
    tgAutoFail: "Backup was created, but Telegram send failed",
    streamFailTitle: "Stream error",
    clearErrorFailTitle: "Error",
    confirmDelete: "Delete this backup?",
    setH2: "Settings",
    setDesc: "Each section stands alone — change only what you need.",
    setSchedTitle: "Automatic schedule",
    setSchedHint: "Choose interval and timezone for automatic backups.",
    lblSchedEnabled: "Enable automatic backups",
    lblSchedInterval: "Interval",
    lblSchedTimezone: "Timezone",
    schedInterval1: "Every 1 hour",
    schedInterval3: "Every 3 hours",
    schedInterval6: "Every 6 hours",
    schedInterval12: "Every 12 hours",
    schedInterval24: "Every 24 hours",
    lblRetention: "Keep last N backups",
    lblRetentionDays: "Max age (days)",
    retentionDaysHint: "0 = count only. Example: 7 deletes archives older than 7 days.",
    lblSchedTelegram: "Also send scheduled backups to Telegram",
    lblSchedNotifyFail: "Notify on schedule failure (Telegram)",
    lblIntegrityVerify: "Verify backup health after create (SHA256 + CRC)",
    schedNextHintEmpty: "Turn on the schedule to see the next run time here.",
    schedNextHint: "Next run: {time}",
    setTgTitle: "Telegram",
    setTgHint: "Optional. The file stays whole on disk and is split only when Telegram needs smaller parts.",
    lblTgEnabled: "Enable Telegram delivery",
    lblTgToken: "Bot Token",
    lblTgChat: "Admin ID / Chat ID",
    lblTgTopic: "Topic ID (optional)",
    tgTopicHint: "For Telegram forum groups; leave empty if unused.",
    lblTgChat2: "Second destination (Chat ID)",
    lblTgTopic2: "Second topic ID",
    tgDestHint: "Optional second chat/topic — both destinations receive the file.",
    lblTgCaption: "Message text",
    tgCaptionHint: "Variables: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgTest: "Connect & send backup",
    setWebhookTitle: "Failure webhook",
    setWebhookHint: "On scheduled backup failure, POST a small JSON payload to this URL.",
    lblWebhookUrl: "Webhook URL (public HTTPS/HTTP)",
    lblWebhookEnabled: "Enable webhook",
    setProxyTitle: "Telegram proxy",
    setProxyHint: "Use a proxy when Telegram is blocked directly.",
    lblProxyEnabled: "Use proxy",
    lblProxyType: "Type",
    lblProxyHost: "Host",
    lblProxyPort: "Port",
    lblProxyUser: "Username",
    lblProxyPass: "Proxy password",
    setPassTitle: "Change panel password",
    setPassHint: "Leave blank if you don’t want to change the password.",
    lblNewPass: "New password",
    lblNewPassConfirm: "Confirm new password",
    btnSaveSettings: "Save settings",
    saved: "Saved",
    streamH2: "Stream to wizard",
    streamDesc: "The zip stays on this server; only a copy is sent to the destination wizard. Put the destination in receive mode first, then send from here.",
    streamStep1: "On destination: Wizard → Restore → Ready to receive stream",
    streamStep2: "Copy the one-time token (valid about 30 minutes)",
    streamStep3: "Paste the destination wizard URL and token here, then start sending",
    streamConnTitle: "Send status",
    streamUrlHint: "Example: http://203.0.113.10:7000 — the port where the destination wizard listens.",
    streamTokenHint: "The token shown after Ready to receive stream on the destination wizard.",
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
    time: "Time",
    healthPg: "PasarGuard",
    healthDocker: "Docker",
    healthDisk: "Backup disk",
    healthMem: "Free RAM",
    healthCpu: "CPU / Load",
    healthArchives: "Backup archives",
    healthSchedule: "Schedule",
    healthOk: "Ready",
    healthMissing: "Not found",
    healthOff: "Off",
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
    panelUrl: "Panel URL",
    ssl: "SSL",
    port: "Port",
    yes: "Yes",
    no: "No",
    lastError: "Last error",
    clearError: "Clear",
    deliveryTitle: "Delivery & schedule",
    profile: "Resource profile",
  },
  ru: {
    logout: "Выход",
    tabDash: "Обзор",
    tabList: "Бэкапы",
    tabSettings: "Настройки",
    authSetupTitle: "Пароль панели бэкапа",
    authSetupDesc: "Первый вход: задайте надёжный пароль для этой панели.",
    lblSetupToken: "Токен установки (одноразовый)",
    authSetupTokenHint: "Вставьте токен, который установщик напечатал в конце.",
    lblCurrentPass: "Текущий пароль",
    errSetupToken: "Неверный токен установки. Скопируйте токен без пробелов.",
    errPassMismatch: "Пароль и подтверждение не совпадают.",
    errPassSet: "Пароль уже задан — используйте вход.",
    errWeakPass: "Слабый пароль. Минимум 12 символов: заглавные, строчные, цифра и спецсимвол.",
    errBadPass: "Неверный пароль.",
    errThrottle: "Слишком много попыток — подождите несколько минут.",
    errServer: "Ошибка сервера. Смотрите: journalctl -u pg-backup -n 50",
    authLoginTitle: "Вход в панель бэкапа",
    authLoginDesc: "Войдите с паролем этой панели.",
    lblPassword: "Пароль",
    lblPasswordConfirm: "Повтор пароля",
    authPolicy: "Минимум 12 символов: заглавные, строчные, цифра и спецсимвол.",
    btnSetup: "Сохранить и войти",
    btnLogin: "Войти",
    dashH2: "Обзор",
    dashDesc: "Статус сервера, панели и последнего бэкапа в одном месте.",
    dashPgTitle: "Статус PasarGuard",
    dashPgSubOk: "Панель установлена на этом сервере и готова к бэкапу.",
    dashPgSubMissing: "PasarGuard не найден. Сначала установите панель на сервере.",
    btnOpenPanel: "Открыть панель PasarGuard",
    updateBannerTitle: "Доступно обновление",
    updateBannerSub: "Вышла версия {latest}. Обновить можно в Настройках.",
    btnUpdateGo: "К обновлению",
    setUpdateTitle: "Обновление панели бэкапа",
    setUpdateHint: "Проверьте GitHub и установите новую версию на этом сервере. Настройки и бэкапы сохраняются.",
    lblUpdateCurrent: "Текущая версия",
    lblUpdateLatest: "Последняя версия",
    lblUpdateChangelog: "Что нового",
    btnUpdateCheck: "Проверить обновления",
    btnUpdateApply: "Установить обновление",
    updateAvailable: "Доступна новая версия.",
    updateUpToDate: "У вас актуальная версия.",
    updateChecking: "Проверка…",
    updateApplying: "Установка обновления…",
    updateDone: "Обновление установлено. Сервис перезапускается.",
    updateFail: "Обновление не удалось",
    updateCheckFail: "Не удалось проверить обновление",
    errorCleared: "Ошибка удалена",
    secHealthTitle: "Здоровье сервера и бэкапа",
    secPgTitle: "Детали панели",
    secStatsTitle: "Живая статистика",
    secBackupStatusTitle: "Бэкап и доставка",
    btnBackupNow: "Сделать полный бэкап сейчас",
    backupRunning: "Создание бэкапа…",
    backupDone: "Бэкап готов",
    backupFail: "Бэкап не удался",
    listH2: "Файлы бэкапов",
    listDesc: "Скачайте файл, отправьте в Telegram или стримом на другой сервер.",
    emptyList: "Бэкапов пока нет. Создайте полный бэкап кнопкой выше.",
    download: "Скачать",
    sendTg: "Telegram",
    sendStream: "Стрим",
    remove: "Удалить",
    latestBackupTag: "Последний",
    backupsPath: "Путь хранения",
    modalOk: "OK",
    modalCancel: "Отмена",
    modalConfirm: "Подтвердить",
    modalNotice: "Сообщение",
    confirmDeleteTitle: "Удалить бэкап",
    tgTestOkTitle: "Тест Telegram",
    tgTestFailTitle: "Ошибка Telegram",
    tgTestOkBody: "Подключено — файл бэкапа отправлен в Telegram",
    tgSendOk: "Файл бэкапа отправлен в Telegram",
    tgSendFail: "Не удалось отправить в Telegram",
    tgAutoSent: "Бэкап также отправлен в Telegram",
    tgAutoFail: "Бэкап создан, но отправка в Telegram не удалась",
    streamFailTitle: "Ошибка стрима",
    clearErrorFailTitle: "Ошибка",
    confirmDelete: "Удалить этот бэкап?",
    setH2: "Настройки",
    setDesc: "Каждый блок отдельный — меняйте только то, что нужно.",
    setSchedTitle: "Авторасписание",
    setSchedHint: "Интервал и часовой пояс автоматического бэкапа.",
    lblSchedEnabled: "Включить автоматический бэкап",
    lblSchedInterval: "Интервал",
    lblSchedTimezone: "Часовой пояс",
    schedInterval1: "Каждый 1 час",
    schedInterval3: "Каждые 3 часа",
    schedInterval6: "Каждые 6 часов",
    schedInterval12: "Каждые 12 часов",
    schedInterval24: "Каждые 24 часа",
    lblRetention: "Хранить последние N",
    lblRetentionDays: "Макс. возраст (дни)",
    retentionDaysHint: "0 = только по количеству. Например 7 — удалять старше 7 дней.",
    lblSchedTelegram: "Также отправлять автобэкап в Telegram",
    lblSchedNotifyFail: "Уведомлять при сбое расписания (Telegram)",
    lblIntegrityVerify: "Проверять целостность после создания (SHA256 + CRC)",
    schedNextHintEmpty: "Включите расписание, чтобы увидеть следующий запуск.",
    schedNextHint: "Следующий запуск: {time}",
    setTgTitle: "Telegram",
    setTgHint: "По желанию. На диске файл остаётся целым; дробится только при отправке в Telegram, если нужно.",
    lblTgEnabled: "Включить отправку в Telegram",
    lblTgToken: "Bot Token",
    lblTgChat: "Admin ID / Chat ID",
    lblTgTopic: "Topic ID (опционально)",
    tgTopicHint: "Для форум-групп Telegram; оставьте пустым, если не нужно.",
    lblTgChat2: "Второе назначение (Chat ID)",
    lblTgTopic2: "Topic второго назначения",
    tgDestHint: "Можно указать второй чат/топик — оба получат файл.",
    lblTgCaption: "Текст сообщения",
    tgCaptionHint: "Переменные: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgTest: "Подключить и отправить бэкап",
    setWebhookTitle: "Webhook при сбое",
    setWebhookHint: "При сбое автобэкапа отправится JSON POST на этот URL.",
    lblWebhookUrl: "URL webhook (публичный HTTPS/HTTP)",
    lblWebhookEnabled: "Включить webhook",
    setProxyTitle: "Прокси Telegram",
    setProxyHint: "Если Telegram недоступен напрямую, укажите прокси.",
    lblProxyEnabled: "Использовать прокси",
    lblProxyType: "Тип",
    lblProxyHost: "Хост",
    lblProxyPort: "Порт",
    lblProxyUser: "Пользователь",
    lblProxyPass: "Пароль прокси",
    setPassTitle: "Смена пароля панели",
    setPassHint: "Оставьте пустым, если пароль менять не нужно.",
    lblNewPass: "Новый пароль",
    lblNewPassConfirm: "Повтор нового пароля",
    btnSaveSettings: "Сохранить настройки",
    saved: "Сохранено",
    streamH2: "Стрим в мастер",
    streamDesc: "ZIP остаётся на этом сервере; на мастер назначения уходит только копия. Сначала включите приём на назначении, потом отправляйте отсюда.",
    streamStep1: "На назначении: Мастер → Restore → Готов принимать стрим",
    streamStep2: "Скопируйте одноразовый токен (действует около 30 минут)",
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
    btnStreamSend: "Начать отправку",
    btnStreamBack: "Назад",
    users: "Пользователи",
    nodes: "Ноды",
    admins: "Админы",
    inbounds: "Inbounds",
    hosts: "Hosts",
    groups: "Groups",
    db: "БД",
    lastBackup: "Последний бэкап",
    noLast: "Пока нет бэкапа",
    size: "Размер",
    time: "Время",
    healthPg: "PasarGuard",
    healthDocker: "Docker",
    healthDisk: "Диск бэкапа",
    healthMem: "Свободная RAM",
    healthCpu: "CPU / Load",
    healthArchives: "Архивы бэкапа",
    healthSchedule: "Расписание",
    healthOk: "Готово",
    healthMissing: "Не найден",
    healthOff: "Выкл",
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
    panelUrl: "URL панели",
    ssl: "SSL",
    port: "Порт",
    yes: "Да",
    no: "Нет",
    lastError: "Последняя ошибка",
    clearError: "Удалить",
    deliveryTitle: "Доставка и расписание",
    profile: "Профиль ресурсов",
  },
};

let lang = localStorage.getItem("pg_backup_lang") || "fa";
let setupMode = false;
let pollTimer = null;
let backupProgressFadeTimer = null;
let backupProgressHideTimer = null;
let _modalResolver = null;
let _toastSeq = 0;

function showToast(message, type = "success", opts = {}) {
  const host = document.getElementById("backupToastHost");
  if (!host || !message) return;
  const kind = ["success", "error", "warning"].includes(type) ? type : "success";
  const el = document.createElement("div");
  el.className = `backup-toast is-${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.dataset.toastId = String(++_toastSeq);
  const msg = document.createElement("div");
  msg.className = "backup-toast-msg";
  msg.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "backup-toast-close";
  close.setAttribute("aria-label", "Close");
  close.textContent = "×";
  const remove = () => {
    if (!el.isConnected) return;
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 280);
  };
  close.addEventListener("click", remove);
  el.appendChild(msg);
  el.appendChild(close);
  host.prepend(el);
  const ttl = typeof opts.ttl === "number" ? opts.ttl : (kind === "error" ? 9000 : 5200);
  if (ttl > 0) setTimeout(remove, ttl);
  // Keep sticky host from stacking forever
  while (host.children.length > 4) host.lastElementChild?.remove();
}

function showBackupModal({ title, body, okText, cancelText, danger = false, showCancel = false }) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("backupModal");
    const titleEl = document.getElementById("backupModalTitle");
    const bodyEl = document.getElementById("backupModalBody");
    const okBtn = document.getElementById("backupModalOk");
    const cancelBtn = document.getElementById("backupModalCancel");
    if (!overlay || !okBtn) {
      resolve(true);
      return;
    }
    if (_modalResolver) {
      _modalResolver(false);
      _modalResolver = null;
    }
    _modalResolver = resolve;
    titleEl.textContent = title || t("modalNotice");
    bodyEl.textContent = body || "";
    okBtn.textContent = okText || t("modalOk");
    okBtn.className = danger ? "btn btn-back" : "btn btn-primary";
    if (showCancel) {
      cancelBtn.classList.remove("hidden");
      cancelBtn.textContent = cancelText || t("modalCancel");
    } else {
      cancelBtn.classList.add("hidden");
    }
    overlay.classList.remove("hidden");
    okBtn.focus();
  });
}

function closeBackupModal(result) {
  const overlay = document.getElementById("backupModal");
  if (overlay) overlay.classList.add("hidden");
  if (_modalResolver) {
    const r = _modalResolver;
    _modalResolver = null;
    r(!!result);
  }
}

function initBackupModal() {
  const overlay = document.getElementById("backupModal");
  const okBtn = document.getElementById("backupModalOk");
  const cancelBtn = document.getElementById("backupModalCancel");
  if (!overlay || overlay.dataset.ready) return;
  overlay.dataset.ready = "1";
  okBtn?.addEventListener("click", () => closeBackupModal(true));
  cancelBtn?.addEventListener("click", () => closeBackupModal(false));
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeBackupModal(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.classList.contains("hidden")) {
      closeBackupModal(false);
    }
  });
}

function t(key) {
  return (I18N[lang] && I18N[lang][key]) || (I18N.en[key] || key);
}

function setBackupLang(next) {
  lang = next;
  localStorage.setItem("pg_backup_lang", next);
  document.documentElement.lang = next;
  document.documentElement.dir = next === "fa" ? "rtl" : "ltr";
  syncLangMenu(next);
  applyI18n();
  if (!document.getElementById("panel-auth")?.classList.contains("active")) {
    refreshDashboard().catch(() => {});
    if (document.getElementById("panel-list")?.classList.contains("active")) {
      refreshList().catch(() => {});
    }
  }
}

const LANG_SHORT = { fa: "FA", en: "EN", ru: "RU" };

function syncLangMenu(code) {
  const current = LANG_SHORT[code] || String(code).toUpperCase();
  const label = document.getElementById("langCurrentLabel");
  if (label) label.textContent = current;
  const menu = document.getElementById("langMenu");
  if (menu) {
    menu.querySelectorAll("[data-lang]").forEach((li) => {
      const on = li.getAttribute("data-lang") === code;
      li.setAttribute("aria-selected", on ? "true" : "false");
      li.classList.toggle("is-active", on);
    });
  }
  const trigger = document.getElementById("langTrigger");
  if (trigger) trigger.setAttribute("aria-expanded", "false");
  document.getElementById("langSwitch")?.classList.remove("is-open");
  menu?.classList.add("hidden");
}

function initLangMenu() {
  const root = document.getElementById("langSwitch");
  const trigger = document.getElementById("langTrigger");
  const menu = document.getElementById("langMenu");
  if (!root || !trigger || !menu || root.dataset.ready) return;
  root.dataset.ready = "1";

  const close = () => {
    root.classList.remove("is-open");
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
  };

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = root.classList.toggle("is-open");
    menu.classList.toggle("hidden", !open);
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
  });

  menu.querySelectorAll("[data-lang]").forEach((li) => {
    li.addEventListener("click", (e) => {
      e.stopPropagation();
      const code = li.getAttribute("data-lang");
      close();
      if (code) setBackupLang(code);
    });
  });

  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

function applyI18n() {
  const map = [
    ["tabDash", "tabDash"], ["tabList", "tabList"], ["tabSettings", "tabSettings"],
    ["btnLogout", "logout"], ["lblPassword", "lblPassword"], ["lblPasswordConfirm", "lblPasswordConfirm"],
    ["authPolicy", "authPolicy"], ["dashPgTitle", "dashPgTitle"],
    ["secHealthTitle", "secHealthTitle"], ["secPgTitle", "secPgTitle"],
    ["secBackupStatusTitle", "secBackupStatusTitle"],
    ["btnBackupNowList", "btnBackupNow"],
    ["listH2", "listH2"], ["listDesc", "listDesc"],
    ["setH2", "setH2"], ["setDesc", "setDesc"], ["setSchedTitle", "setSchedTitle"], ["setSchedHint", "setSchedHint"],
    ["lblSchedInterval", "lblSchedInterval"], ["lblSchedTimezone", "lblSchedTimezone"],
    ["lblRetention", "lblRetention"], ["lblRetentionDays", "lblRetentionDays"], ["retentionDaysHint", "retentionDaysHint"],
    ["lblSchedTelegram", "lblSchedTelegram"], ["lblSchedNotifyFail", "lblSchedNotifyFail"],
    ["lblIntegrityVerify", "lblIntegrityVerify"],
    ["setTgTitle", "setTgTitle"], ["setTgHint", "setTgHint"], ["lblTgToken", "lblTgToken"],
    ["lblTgChat", "lblTgChat"], ["lblTgTopic", "lblTgTopic"], ["tgTopicHint", "tgTopicHint"],
    ["lblTgChat2", "lblTgChat2"], ["lblTgTopic2", "lblTgTopic2"], ["tgDestHint", "tgDestHint"],
    ["lblTgCaption", "lblTgCaption"], ["tgCaptionHint", "tgCaptionHint"],
    ["btnTgTest", "btnTgTest"],
    ["setWebhookTitle", "setWebhookTitle"], ["setWebhookHint", "setWebhookHint"], ["lblWebhookUrl", "lblWebhookUrl"],
    ["setProxyTitle", "setProxyTitle"], ["setProxyHint", "setProxyHint"],
    ["lblProxyType", "lblProxyType"], ["lblProxyHost", "lblProxyHost"], ["lblProxyPort", "lblProxyPort"],
    ["lblProxyUser", "lblProxyUser"], ["lblProxyPass", "lblProxyPass"],
    ["setUpdateTitle", "setUpdateTitle"], ["setUpdateHint", "setUpdateHint"],
    ["lblUpdateCurrent", "lblUpdateCurrent"], ["lblUpdateLatest", "lblUpdateLatest"],
    ["lblUpdateChangelog", "lblUpdateChangelog"],
    ["btnUpdateCheck", "btnUpdateCheck"], ["btnUpdateApply", "btnUpdateApply"],
    ["btnDashUpdateGo", "btnUpdateGo"], ["btnOpenPanel", "btnOpenPanel"],
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
    ["schedNotifyFail", "lblSchedNotifyFail"],
    ["integrityVerify", "lblIntegrityVerify"],
    ["tgEnabled", "lblTgEnabled"],
    ["webhookEnabled", "lblWebhookEnabled"],
    ["proxyEnabled", "lblProxyEnabled"],
  ];
  for (const [id, key] of switchLabels) {
    const el = document.getElementById(id);
    if (el) el.setAttribute("aria-label", t(key));
  }
  initProxyTypeSelect();
  initSchedIntervalSelect();
  initSchedTimezoneSelect();
  initLangMenu();
  initBackupModal();
  syncLangMenu(lang);
  const clearBtn = document.getElementById("btnClearLastError");
  if (clearBtn) clearBtn.textContent = t("clearError");
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

/** Simplify ISO/UTC timestamps like 2026-08-29T12:34:56Z → 2026/08/29 12:34 */
function formatSimpleTime(iso) {
  if (!iso) return "—";
  const s = String(iso).trim();
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (m) return `${m[1]}/${m[2]}/${m[3]} ${m[4]}:${m[5]}`;
  return s.replace("T", " ").replace(/Z$/, "").replace(/\.\d+/, "").slice(0, 16);
}

function metricCard({ tone, icon, label, value, sub, subHtml, status, valueLtr = true, hintLtr = false }) {
  const statusCls = status ? ` is-${status}` : "";
  const valueDir = valueLtr ? ' dir="ltr"' : "";
  const hintDir = hintLtr ? ' dir="ltr"' : "";
  let hint = "";
  if (subHtml) {
    hint = `<p class="backup-health-hint">${subHtml}</p>`;
  } else if (sub) {
    hint = `<p class="backup-health-hint"${hintDir}>${esc(sub)}</p>`;
  }
  return `<article class="backup-health-card">
    <span class="choice-icon ${tone}" aria-hidden="true">${icon}</span>
    <div class="backup-health-body">
      <p class="backup-health-label">${esc(label)}</p>
      <p class="backup-health-value${statusCls}"${valueDir}>${esc(value)}</p>
      ${hint}
    </div>
  </article>`;
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
  document.getElementById("appVersion").textContent = "v" + (st.version || "4.2.3");
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
    ? "success-card backup-section-card backup-pg-card"
    : "warning-card backup-section-card backup-pg-card";
  document.getElementById("dashPgSub").textContent = installed ? t("dashPgSubOk") : t("dashPgSubMissing");

  const specs = document.getElementById("dashPgSpecs");
  const rootPath = access.root_path || "/";
  specs.innerHTML = [
    ["PasarGuard", installed ? "OK" : "—"],
    [t("db"), access.db_type || sys.pasarguard_db || "—"],
    [t("port"), access.port || "—"],
    ["Path", rootPath],
    [t("ssl"), access.ssl == null ? "—" : (access.ssl ? t("yes") : t("no"))],
    ["Host", access.host || access.domain || access.ip || "—"],
  ].map(([label, value]) => `
    <div class="specs-item">
      <span class="specs-label">${esc(label)}</span>
      <span class="specs-value" title="${esc(value)}">${esc(value)}</span>
    </div>
  `).join("");

  const actions = document.getElementById("dashPgActions");
  const btnPanel = document.getElementById("btnOpenPanel");
  const urlEl = document.getElementById("dashPgUrl");
  const panelUrl = access.login_url || access.url || access.public_url || access.public_http_url || "";
  if (actions && btnPanel && urlEl) {
    if (installed && panelUrl) {
      actions.hidden = false;
      btnPanel.href = panelUrl;
      btnPanel.textContent = t("btnOpenPanel");
      urlEl.textContent = panelUrl;
    } else {
      actions.hidden = true;
      btnPanel.removeAttribute("href");
      urlEl.textContent = "";
    }
  }

  renderUpdateBanner(data.update || {});
  const diskFree = health.backup_disk_free_bytes;
  const diskTotal = health.backup_disk_total_bytes;
  const memFree = health.memory_available_bytes;
  const load = health.load_ratio_1m;
  const schedInterval = [1, 3, 6, 12, 24].includes(Number(sched.interval_hours))
    ? Number(sched.interval_hours)
    : 24;
  const schedLabel = t(`schedInterval${schedInterval}`);
  const nextLocal = (sched.next_run && sched.next_run.at_local) || sched.last_success_local || "";
  const tz = sched.timezone || "UTC";
  document.getElementById("dashHealthGrid").innerHTML = [
    metricCard({
      tone: "icon-tone-blue",
      icon: ICONS.shield,
      label: t("healthPg"),
      value: installed ? t("healthOk") : t("healthMissing"),
      status: installed ? "ok" : "warn",
      valueLtr: false,
      sub: access.db_type || sys.pasarguard_db || "—",
      hintLtr: true,
    }),
    metricCard({
      tone: "icon-tone-cyan",
      icon: ICONS.docker,
      label: t("healthDocker"),
      value: sys.docker ? t("healthOk") : t("healthMissing"),
      status: sys.docker ? "ok" : "off",
      valueLtr: false,
      subHtml: `${esc(t("profile"))}: <bdi dir="ltr">${esc(health.profile || "—")}</bdi>`,
    }),
    metricCard({
      tone: "icon-tone-orange",
      icon: ICONS.disk,
      label: t("healthDisk"),
      value: humanSize(diskFree),
      subHtml: diskTotal != null
        ? `${esc(t("freeOf"))} <bdi dir="ltr">${esc(humanSize(diskTotal))}</bdi>`
        : "",
    }),
    metricCard({
      tone: "icon-tone-green",
      icon: ICONS.cpu,
      label: t("healthMem"),
      value: memFree != null ? humanSize(memFree) : "—",
      subHtml: health.cpu_count != null
        ? `<bdi dir="ltr">CPU ${esc(health.cpu_count)} · load ${esc(load ?? "—")}</bdi>`
        : "",
    }),
    metricCard({
      tone: "icon-tone-yellow",
      icon: ICONS.archive,
      label: t("healthArchives"),
      value: String(data.backup_count ?? 0),
      subHtml: `${esc(t("totalSize"))}: <bdi dir="ltr">${esc(humanSize(data.backup_total_bytes))}</bdi>`,
    }),
    metricCard({
      tone: "icon-tone-blue",
      icon: ICONS.clock,
      label: t("healthSchedule"),
      value: sched.enabled ? schedLabel : t("healthOff"),
      status: sched.enabled ? "ok" : "off",
      valueLtr: false,
      subHtml: sched.enabled
        ? `<bdi dir="ltr">${esc(tz)}</bdi>${nextLocal ? ` · <bdi dir="ltr">${esc(nextLocal)}</bdi>` : ""}`
        : `${esc(t("keepLast"))}: <bdi dir="ltr">${esc(data.retention_count || 10)}</bdi>`,
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
      <div class="specs-grid backup-last-specs">
        <div class="specs-item"><span class="specs-label">${esc(t("size"))}</span><span class="specs-value" dir="ltr">${esc(humanSize(last.size_bytes))}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("db"))}</span><span class="specs-value">${esc(last.db_type || "—")}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("users"))}</span><span class="specs-value" dir="ltr">${esc(c.users ?? "—")}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("nodes"))}</span><span class="specs-value" dir="ltr">${esc(c.nodes ?? "—")}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("admins"))}</span><span class="specs-value" dir="ltr">${esc(c.admins ?? "—")}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("inbounds"))}</span><span class="specs-value" dir="ltr">${esc(c.inbounds ?? "—")}</span></div>
        <div class="specs-item"><span class="specs-label">${esc(t("time"))}</span><span class="specs-value" dir="ltr">${esc(formatSimpleTime(last.created_at))}</span></div>
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
        ${sched.enabled ? `<span class="backup-chip">${esc(schedLabel)}</span>` : ""}
        <span class="backup-chip">${esc(tg.proxy_enabled ? t("proxyOn") : "Proxy —")}</span>
      </div>`;

  setTelegramStatusTag({ connected: !!tgLive.connected });

  const errBox = document.getElementById("dashError");
  const errBody = document.getElementById("dashErrorBody");
  const clearBtn = document.getElementById("btnClearLastError");
  if (clearBtn) clearBtn.textContent = t("clearError");
  if (data.last_error && data.last_error.message) {
    errBox.classList.remove("hidden");
    if (errBody) {
      errBody.innerHTML = `<strong>${esc(t("lastError"))}</strong><br>${esc(data.last_error.at || "")} · ${esc(data.last_error.message)}`;
    }
  } else {
    errBox.classList.add("hidden");
    if (errBody) errBody.textContent = "";
  }
}

async function createBackupNow() {
  const box = document.getElementById("backupProgress");
  const title = document.getElementById("backupProgressTitle");
  const logEl = document.getElementById("backupProgressLog");
  const track = box?.querySelector(".backup-progress-track");
  const bar = document.getElementById("backupProgressBar");
  const pctEl = document.getElementById("backupProgressPct");
  const listBtn = document.getElementById("btnBackupNowList");
  showBackupTab("list");
  clearTimeout(backupProgressFadeTimer);
  clearTimeout(backupProgressHideTimer);
  box.classList.remove("hidden", "is-success", "is-error", "is-fading", "is-success-compact");
  box.classList.add("is-running");
  title.textContent = t("backupRunning");
  logEl.classList.remove("hidden");
  track?.classList.remove("hidden");
  logEl.textContent = "";
  setBackupProgressUI(0);
  if (listBtn) listBtn.disabled = true;
  try {
    const job = await api("/api/backups/create", { method: "POST", body: "{}" });
    const done = await pollJob(job.job_id, title, logEl, box);
    box.classList.remove("is-running");
    box.classList.add("is-success", "is-success-compact");
    setBackupProgressUI(100);
    title.textContent = t("backupDone") + (done.filename ? ": " + done.filename : "");
    logEl.classList.add("hidden");
    track?.classList.add("hidden");
    if (pctEl) pctEl.classList.add("hidden");
    const tg = done.telegram;
    if (tg && tg.ok) {
      showToast(t("backupDone") + (done.filename ? ": " + done.filename : "") + "\n" + t("tgAutoSent"), "success");
    } else if (tg && tg.ok === false) {
      showToast(t("tgAutoFail") + (tg.error ? ": " + tg.error : ""), "warning");
    } else {
      showToast(t("backupDone") + (done.filename ? ": " + done.filename : ""), "success");
    }
    await refreshDashboard();
    await refreshList();
    backupProgressFadeTimer = setTimeout(() => {
      box.classList.add("is-fading");
      backupProgressHideTimer = setTimeout(() => {
        box.classList.add("hidden");
        box.classList.remove("is-fading", "is-success", "is-success-compact", "is-error", "is-running");
        logEl.classList.remove("hidden");
        track?.classList.remove("hidden");
        pctEl?.classList.remove("hidden");
        setBackupProgressUI(0);
      }, 550);
    }, 4200);
  } catch (e) {
    box.classList.remove("is-running", "is-success-compact");
    box.classList.add("is-error");
    title.textContent = t("backupFail") + ": " + e.message;
    logEl.classList.remove("hidden");
    track?.classList.remove("hidden");
    pctEl?.classList.remove("hidden");
    showToast(t("backupFail") + ": " + e.message, "error");
  } finally {
    if (listBtn) listBtn.disabled = false;
  }
}

function setBackupProgressUI(pct) {
  const bar = document.getElementById("backupProgressBar");
  const pctEl = document.getElementById("backupProgressPct");
  const n = Math.max(0, Math.min(100, Number(pct) || 0));
  if (bar) {
    bar.style.width = n + "%";
    bar.classList.add("is-green");
  }
  if (pctEl) pctEl.textContent = Math.round(n) + "%";
}

async function pollJob(jobId, title, logEl, box) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const job = await api("/api/backups/jobs/" + jobId);
        logEl.textContent = (job.logs || []).join("\n");
        logEl.scrollTop = logEl.scrollHeight;
        const pct = Number(job.progress);
        if (!Number.isNaN(pct)) setBackupProgressUI(pct);
        if (job.phase && job.status === "running") {
          title.textContent = t("backupRunning") + " · " + job.phase;
        }
        if (job.status === "success") {
          setBackupProgressUI(100);
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
        pollTimer = setTimeout(tick, 700);
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
  const pathEl = document.getElementById("listBackupPath");
  if (pathEl) {
    const p = data.backups_path || "";
    if (p) {
      pathEl.hidden = false;
      pathEl.textContent = `${t("backupsPath")}: ${p}`;
    } else {
      pathEl.hidden = true;
      pathEl.textContent = "";
    }
  }
  const items = data.items || [];
  if (!items.length) {
    root.innerHTML = `<div class="info-box backup-empty">
      <span class="choice-icon icon-tone-yellow" aria-hidden="true">${ICONS.empty}</span>
      <p>${t("emptyList")}</p>
    </div>`;
    return;
  }
  root.innerHTML = items.map((it, idx) => {
    const m = it.manifest || {};
    const c = m.counts || {};
    const latest = idx === 0
      ? `<span class="backup-latest-tag">${esc(t("latestBackupTag"))}</span>`
      : "";
    return `<div class="backup-item${idx === 0 ? " is-latest" : ""}">
      <div class="backup-item-head">
        <div style="display:flex;gap:12px;align-items:flex-start;min-width:0">
          <span class="choice-icon icon-tone-blue" aria-hidden="true">${ICONS.archive}</span>
          <div style="min-width:0">
            <strong>${esc(it.filename)}${latest}</strong>
            <div class="backup-item-meta" dir="ltr">${esc(formatSimpleTime(it.mtime) || it.mtime || "")} · ${esc(m.db_type || "?")}</div>
          </div>
        </div>
        <span class="backup-item-badge" dir="ltr">${humanSize(it.size_bytes)}</span>
      </div>
      <div class="backup-item-chips">
        <span class="backup-chip">${t("users")}: ${c.users ?? "—"}</span>
        <span class="backup-chip">${t("nodes")}: ${c.nodes ?? "—"}</span>
        <span class="backup-chip">${t("admins")}: ${c.admins ?? "—"}</span>
        <span class="backup-chip">${t("inbounds")}: ${c.inbounds ?? "—"}</span>
      </div>
      <div class="backup-item-actions">
        <a class="btn-list-action is-download" href="/api/backups/${encodeURIComponent(it.id)}/download">${ICONS.download}<span>${t("download")}</span></a>
        <button type="button" class="btn-list-action is-tg" onclick="sendTelegram('${it.id}')">${ICONS.send}<span>${t("sendTg")}</span></button>
        <button type="button" class="btn-list-action is-stream" onclick="openStream('${it.id}')">${ICONS.stream}<span>${t("sendStream")}</span></button>
        <button type="button" class="btn-list-action is-delete" onclick="deleteBackup('${it.id}')">${ICONS.trash}<span>${t("remove")}</span></button>
      </div>
    </div>`;
  }).join("");
}

async function sendTelegram(id) {
  try {
    const r = await api("/api/backups/" + encodeURIComponent(id) + "/telegram", { method: "POST", body: "{}" });
    showToast(
      r.ok
        ? t("tgSendOk") + (r.parts ? ` · parts=${r.parts}` : "")
        : (r.error || t("tgSendFail")),
      r.ok ? "success" : "error",
    );
  } catch (e) {
    showToast(t("tgSendFail") + ": " + e.message, "error");
  }
}

async function deleteBackup(id) {
  const ok = await showBackupModal({
    title: t("confirmDeleteTitle"),
    body: t("confirmDelete"),
    okText: t("modalConfirm"),
    showCancel: true,
    danger: true,
  });
  if (!ok) return;
  await api("/api/backups/" + encodeURIComponent(id), { method: "DELETE" });
  refreshList();
  refreshDashboard();
}

async function openStream(id) {
  document.getElementById("streamBackupId").value = id;
  try {
    const settings = await api("/api/settings");
    document.getElementById("streamUrl").value = (settings.stream && settings.stream.default_dest_url) || "";
  } catch (_) {
    document.getElementById("streamUrl").value = "";
  }
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
    showToast(t("streamFail") + ": " + e.message, "error");
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
          showToast(msg.textContent, "success");
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
  setSchedIntervalValue(sched.interval_hours ?? 24);
  setSchedTimezoneValue(sched.timezone || "UTC");
  document.getElementById("retentionCount").value = s.retention_count ?? 10;
  document.getElementById("retentionDays").value = s.retention_days ?? 0;
  document.getElementById("schedTelegram").checked = !!sched.send_telegram;
  document.getElementById("schedNotifyFail").checked = sched.notify_on_failure !== false;
  document.getElementById("integrityVerify").checked = !!(s.integrity && s.integrity.verify_after_create !== false);
  const nextEl = document.getElementById("schedNextHint");
  if (nextEl) {
    if (sched.enabled && sched.next_run && sched.next_run.at_local) {
      nextEl.textContent = t("schedNextHint").replace("{time}", sched.next_run.at_local);
    } else {
      nextEl.textContent = t("schedNextHintEmpty");
    }
  }
  document.getElementById("tgEnabled").checked = !!tg.enabled;
  document.getElementById("tgToken").value = "";
  document.getElementById("tgToken").placeholder = tg.bot_token_hint || "";
  document.getElementById("tgTokenHint").textContent = tg.bot_token_set ? (tg.bot_token_hint || "••••") : "";
  document.getElementById("tgChat").value = tg.admin_id || tg.chat_id || "";
  document.getElementById("tgTopic").value = tg.message_thread_id || "";
  const extras = tg.extra_destinations || [];
  document.getElementById("tgChat2").value = (extras[0] && extras[0].chat_id) || "";
  document.getElementById("tgTopic2").value = (extras[0] && extras[0].message_thread_id) || "";
  document.getElementById("tgCaption").value = tg.caption_template || "";
  const notify = s.notify || {};
  document.getElementById("webhookEnabled").checked = !!notify.webhook_enabled;
  document.getElementById("webhookUrl").value = notify.webhook_url || "";
  document.getElementById("proxyEnabled").checked = !!tg.proxy_enabled;
  setProxyTypeValue(tg.proxy_type || "socks5");
  document.getElementById("proxyHost").value = tg.proxy_host || "";
  document.getElementById("proxyPort").value = tg.proxy_port || 1080;
  document.getElementById("proxyUser").value = tg.proxy_user || "";
  document.getElementById("proxyPass").value = "";
  setTelegramStatusTag({ checking: true });
  refreshTelegramStatusTag().catch(() => setTelegramStatusTag({ connected: false }));
  checkForUpdate(false).catch(() => {});
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

async function saveSettings(opts = {}) {
  const quiet = !!opts.quiet;
  const tokenVal = document.getElementById("tgToken").value.trim();
  const adminId = document.getElementById("tgChat").value.trim();
  const extraChat = document.getElementById("tgChat2").value.trim();
  const extraTopicRaw = document.getElementById("tgTopic2").value.trim();
  const destinations = [];
  if (extraChat) {
    const item = { chat_id: extraChat, label: "secondary" };
    if (extraTopicRaw) item.message_thread_id = Number(extraTopicRaw);
    destinations.push(item);
  }
  const topicRaw = document.getElementById("tgTopic").value.trim();
  const patch = {
    retention_count: Number(document.getElementById("retentionCount").value || 10),
    retention_days: Number(document.getElementById("retentionDays").value || 0),
    schedule: {
      enabled: document.getElementById("schedEnabled").checked,
      interval_hours: Number(document.getElementById("schedInterval").value || 24),
      timezone: document.getElementById("schedTimezone").value || "UTC",
      send_telegram: document.getElementById("schedTelegram").checked,
      notify_on_failure: document.getElementById("schedNotifyFail").checked,
    },
    integrity: {
      verify_after_create: document.getElementById("integrityVerify").checked,
    },
    notify: {
      webhook_enabled: document.getElementById("webhookEnabled").checked,
      webhook_url: document.getElementById("webhookUrl").value.trim(),
    },
    telegram: {
      enabled: document.getElementById("tgEnabled").checked,
      bot_token: tokenVal,
      chat_id: adminId,
      admin_id: adminId,
      message_thread_id: topicRaw ? Number(topicRaw) : null,
      destinations,
      caption_template: document.getElementById("tgCaption").value,
      proxy_enabled: document.getElementById("proxyEnabled").checked,
      proxy_type: document.getElementById("proxyType").value,
      proxy_host: document.getElementById("proxyHost").value.trim(),
      proxy_port: Number(document.getElementById("proxyPort").value || 1080),
      proxy_user: document.getElementById("proxyUser").value.trim(),
      proxy_password: document.getElementById("proxyPass").value,
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
  if (!quiet) showToast(t("saved"), "success");
  refreshTelegramStatusTag().catch(() => {});
}

async function testTelegram() {
  const btn = document.getElementById("btnTgTest");
  if (btn) btn.disabled = true;
  try {
    document.getElementById("tgEnabled").checked = true;
    await saveSettings({ quiet: true });
    showToast(t("backupRunning"), "warning", { ttl: 4000 });
    const r = await api("/api/telegram/test", { method: "POST", body: "{}" });
    setTelegramStatusTag({ connected: true });
    const bot = (r.bot && r.bot.username) ? (" @" + r.bot.username) : "";
    const file = (r.backup && r.backup.filename) ? ("\n" + r.backup.filename) : "";
    showToast(t("tgTestOkBody") + bot + file, "success", { ttl: 8000 });
    await refreshList();
    await refreshDashboard();
    await loadSettingsForm();
  } catch (e) {
    setTelegramStatusTag({ connected: false });
    showToast(t("tgTestFailTitle") + ": " + e.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}


function setProxyTypeValue(value) {
  const v = (value === "http") ? "http" : "socks5";
  const hidden = document.getElementById("proxyType");
  const label = document.getElementById("proxyTypeLabel");
  const menu = document.getElementById("proxyTypeMenu");
  if (hidden) hidden.value = v;
  if (label) label.textContent = v.toUpperCase();
  if (menu) {
    menu.querySelectorAll("[role=option]").forEach((li) => {
      const on = li.getAttribute("data-value") === v;
      li.setAttribute("aria-selected", on ? "true" : "false");
      li.classList.toggle("is-active", on);
    });
  }
}

const SCHED_INTERVALS = [1, 3, 6, 12, 24];

function schedIntervalLabel(hours) {
  const n = SCHED_INTERVALS.includes(Number(hours)) ? Number(hours) : 24;
  return t(`schedInterval${n}`);
}

function setSchedIntervalValue(value) {
  const n = SCHED_INTERVALS.includes(Number(value)) ? Number(value) : 24;
  const hidden = document.getElementById("schedInterval");
  const label = document.getElementById("schedIntervalLabel");
  const menu = document.getElementById("schedIntervalMenu");
  if (hidden) hidden.value = String(n);
  if (label) label.textContent = schedIntervalLabel(n);
  if (menu) {
    menu.querySelectorAll("[role=option]").forEach((li) => {
      const on = Number(li.getAttribute("data-value")) === n;
      li.setAttribute("aria-selected", on ? "true" : "false");
      li.classList.toggle("is-active", on);
      li.textContent = schedIntervalLabel(li.getAttribute("data-value"));
    });
  }
}

function initSchedIntervalSelect() {
  const root = document.getElementById("schedIntervalSelect");
  const trigger = document.getElementById("schedIntervalTrigger");
  const menu = document.getElementById("schedIntervalMenu");
  if (!root || !trigger || !menu) return;
  const current = document.getElementById("schedInterval")?.value || "24";
  if (root.dataset.ready) {
    setSchedIntervalValue(current);
    return;
  }
  root.dataset.ready = "1";

  const close = () => {
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
    root.classList.remove("is-open");
  };
  const open = () => {
    menu.classList.remove("hidden");
    trigger.setAttribute("aria-expanded", "true");
    root.classList.add("is-open");
  };

  trigger.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (menu.classList.contains("hidden")) open();
    else close();
  });
  menu.querySelectorAll("[role=option]").forEach((li) => {
    li.addEventListener("click", (e) => {
      e.preventDefault();
      setSchedIntervalValue(li.getAttribute("data-value"));
      close();
    });
  });
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
  setSchedIntervalValue(current);
}

const SCHED_TIMEZONES = [
  "UTC", "Asia/Tehran", "Asia/Dubai", "Asia/Istanbul", "Europe/Moscow",
  "Europe/London", "Europe/Berlin", "America/New_York", "Asia/Shanghai", "Asia/Tokyo",
];

function setSchedTimezoneValue(value) {
  const v = SCHED_TIMEZONES.includes(value) ? value : "UTC";
  const hidden = document.getElementById("schedTimezone");
  const label = document.getElementById("schedTimezoneLabel");
  const menu = document.getElementById("schedTimezoneMenu");
  if (hidden) hidden.value = v;
  if (label) label.textContent = v;
  if (menu) {
    menu.querySelectorAll("[role=option]").forEach((li) => {
      const on = li.getAttribute("data-value") === v;
      li.setAttribute("aria-selected", on ? "true" : "false");
      li.classList.toggle("is-active", on);
    });
  }
}

function initSchedTimezoneSelect() {
  const root = document.getElementById("schedTimezoneSelect");
  const trigger = document.getElementById("schedTimezoneTrigger");
  const menu = document.getElementById("schedTimezoneMenu");
  if (!root || !trigger || !menu) return;
  const current = document.getElementById("schedTimezone")?.value || "UTC";
  if (root.dataset.ready) {
    setSchedTimezoneValue(current);
    return;
  }
  root.dataset.ready = "1";

  const close = () => {
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
    root.classList.remove("is-open");
  };
  const open = () => {
    menu.classList.remove("hidden");
    trigger.setAttribute("aria-expanded", "true");
    root.classList.add("is-open");
  };

  trigger.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (menu.classList.contains("hidden")) open();
    else close();
  });
  menu.querySelectorAll("[role=option]").forEach((li) => {
    li.addEventListener("click", (e) => {
      e.preventDefault();
      setSchedTimezoneValue(li.getAttribute("data-value"));
      close();
    });
  });
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
  setSchedTimezoneValue(current);
}

function initProxyTypeSelect() {
  const root = document.getElementById("proxyTypeSelect");
  const trigger = document.getElementById("proxyTypeTrigger");
  const menu = document.getElementById("proxyTypeMenu");
  if (!root || !trigger || !menu || root.dataset.ready) return;
  root.dataset.ready = "1";

  const close = () => {
    menu.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
    root.classList.remove("is-open");
  };
  const open = () => {
    menu.classList.remove("hidden");
    trigger.setAttribute("aria-expanded", "true");
    root.classList.add("is-open");
  };

  trigger.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (menu.classList.contains("hidden")) open();
    else close();
  });
  menu.querySelectorAll("[role=option]").forEach((li) => {
    li.addEventListener("click", (e) => {
      e.preventDefault();
      setProxyTypeValue(li.getAttribute("data-value"));
      close();
    });
  });
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
  setProxyTypeValue(document.getElementById("proxyType")?.value || "socks5");
}

async function clearLastError() {
  try {
    await api("/api/dashboard/last-error", { method: "DELETE" });
    const errBox = document.getElementById("dashError");
    const errBody = document.getElementById("dashErrorBody");
    if (errBox) errBox.classList.add("hidden");
    if (errBody) errBody.textContent = "";
    showToast(t("errorCleared"), "success");
  } catch (e) {
    showToast(t("clearErrorFailTitle") + ": " + e.message, "error");
  }
}

function renderUpdateBanner(info) {
  const banner = document.getElementById("dashUpdateBanner");
  if (!banner) return;
  if (!info || !info.available) {
    banner.classList.add("hidden");
    return;
  }
  const latest = info.latest || info.latest_tag || "";
  document.getElementById("dashUpdateTitle").textContent = t("updateBannerTitle");
  document.getElementById("dashUpdateSub").textContent = t("updateBannerSub").replace("{latest}", latest);
  document.getElementById("btnDashUpdateGo").textContent = t("btnUpdateGo");
  banner.classList.remove("hidden");
}

function goUpdateSettings() {
  showBackupTab("settings");
  const card = document.getElementById("setUpdateCard");
  if (card) {
    setTimeout(() => card.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }
  checkForUpdate(true).catch(() => {});
}

function renderUpdatePanel(info) {
  const cur = document.getElementById("updateCurrentVer");
  const lat = document.getElementById("updateLatestVer");
  const status = document.getElementById("updateStatusText");
  const wrap = document.getElementById("updateChangelogWrap");
  const body = document.getElementById("updateChangelog");
  const applyBtn = document.getElementById("btnUpdateApply");
  if (cur) cur.textContent = info.current ? ("v" + String(info.current).replace(/^v/i, "")) : "—";
  if (lat) lat.textContent = info.latest ? ("v" + String(info.latest).replace(/^v/i, "")) : "—";
  if (status) {
    if (info.error && !info.available) status.textContent = t("updateCheckFail") + ": " + info.error;
    else if (info.available) status.textContent = t("updateAvailable");
    else status.textContent = t("updateUpToDate");
  }
  if (wrap && body) {
    const text = (info.body || "").trim();
    if (text && info.available) {
      wrap.classList.remove("hidden");
      body.textContent = text;
    } else if (text) {
      wrap.classList.remove("hidden");
      body.textContent = text;
    } else {
      wrap.classList.add("hidden");
      body.textContent = "";
    }
  }
  if (applyBtn) applyBtn.disabled = !info.available;
}

async function checkForUpdate(force = false) {
  const status = document.getElementById("updateStatusText");
  if (status) status.textContent = t("updateChecking");
  try {
    const info = await api("/api/update/status" + (force ? "?force=true" : ""));
    renderUpdatePanel(info);
    renderUpdateBanner(info);
    return info;
  } catch (e) {
    if (status) status.textContent = t("updateCheckFail") + ": " + e.message;
    showToast(t("updateCheckFail") + ": " + e.message, "error");
    return null;
  }
}

function setUpdateProgressUI(pct, title, logs) {
  const box = document.getElementById("updateProgress");
  const bar = document.getElementById("updateProgressBar");
  const pctEl = document.getElementById("updateProgressPct");
  const titleEl = document.getElementById("updateProgressTitle");
  const logEl = document.getElementById("updateProgressLog");
  if (!box) return;
  box.classList.remove("hidden");
  const n = Math.max(0, Math.min(100, Number(pct) || 0));
  if (bar) {
    bar.style.setProperty("width", n + "%", "important");
  }
  if (pctEl) pctEl.textContent = Math.round(n) + "%";
  if (titleEl && title) titleEl.textContent = title;
  if (logEl && logs) {
    logEl.textContent = (logs || []).join("\n");
    logEl.scrollTop = logEl.scrollHeight;
  }
}

async function applyPanelUpdate() {
  const applyBtn = document.getElementById("btnUpdateApply");
  const checkBtn = document.getElementById("btnUpdateCheck");
  if (applyBtn) applyBtn.disabled = true;
  if (checkBtn) checkBtn.disabled = true;
  const box = document.getElementById("updateProgress");
  box?.classList.remove("hidden", "is-success", "is-error");
  box?.classList.add("is-running");
  setUpdateProgressUI(5, t("updateApplying"), []);
  try {
    // Backend returns a running job immediately (work is backgrounded).
    let job = await api("/api/update/apply", { method: "POST", body: "{}" });
    while (job && (job.status === "running" || job.status === "queued")) {
      const pct = Number(job.progress);
      setUpdateProgressUI(Number.isFinite(pct) ? pct : 5, t("updateApplying"), job.logs || []);
      await new Promise((r) => setTimeout(r, 700));
      try {
        job = await api("/api/update/job");
      } catch (_) {
        // service may be restarting — treat as success path
        job = {
          status: "success",
          progress: 100,
          restart_scheduled: true,
          logs: (job && job.logs) || [],
        };
        break;
      }
    }
    box?.classList.remove("is-running");
    if (job && job.status === "success") {
      box?.classList.add("is-success");
      setUpdateProgressUI(100, t("updateDone"), job.logs || []);
      showToast(t("updateDone"), "success", { ttl: 10000 });
      setTimeout(() => location.reload(), 3500);
    } else {
      box?.classList.add("is-error");
      const err = (job && job.error) || "update_failed";
      setUpdateProgressUI(100, t("updateFail") + ": " + err, (job && job.logs) || []);
      showToast(t("updateFail") + ": " + err, "error");
      if (applyBtn) applyBtn.disabled = false;
    }
  } catch (e) {
    box?.classList.remove("is-running");
    box?.classList.add("is-error");
    setUpdateProgressUI(100, t("updateFail") + ": " + e.message, []);
    showToast(t("updateFail") + ": " + e.message, "error");
    if (applyBtn) applyBtn.disabled = false;
  } finally {
    if (checkBtn) checkBtn.disabled = false;
  }
}


boot().catch((e) => {
  const err = document.getElementById("authError");
  err.textContent = e.message || String(e);
  err.classList.remove("hidden");
  showPanel("panel-auth");
});
