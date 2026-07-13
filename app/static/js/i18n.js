/* PG-Migrator i18n — EN / FA / RU */

const I18N = {
  en: {
    title: 'PG-Migrator',
    subtitle: 'Migrate to PasarGuard',
    steps: ['Prerequisites', 'Source Panel', 'Source DB', 'Target DB', 'Confirm', 'Migrate', 'Result'],
    step0: {
      h2: 'Before You Start',
      desc: 'Read what must be installed on this server BEFORE migration.',
      info: 'This wizard runs on your Ubuntu server with root access. What you need depends on the source panel — details appear after you select one.',
      checks: [
        ['🖥️', 'Ubuntu server', 'This wizard runs on port 7000'],
        ['🔑', 'Root access', 'Required to modify .env and Docker'],
        ['💾', 'Backup', 'Always backup before migrating'],
      ],
      start: 'Continue →',
      pasarguardCheck: 'PasarGuard on server',
      pasarguardYes: 'Installed',
      pasarguardNo: 'Not installed — install manually before migration (if required)',
      marzbanCheck: 'Marzban on server',
      marzbanYes: 'Installed',
      marzbanNo: 'Not installed',
      dockerCheck: 'Docker',
      dockerYes: 'Running',
      dockerNo: 'Not running',
      checking: 'Checking server...',
      checkingDetail: 'Detecting PasarGuard, Marzban, Docker',
    },
    step1: {
      h2: 'Select Source Panel',
      desc: 'Which panel are you migrating from?',
      back: '← Back',
      next: 'Continue →',
      prereqTitle: 'What must be installed:',
      uploadHint: 'Missing something? You can upload a backup in the next step.',
      marzbanModeTitle: 'Marzban migration method',
      marzbanModeDesc: 'Choose how to migrate based on your server setup (per official PasarGuard docs).',
      marzbanInplace: 'In-place (Marzban on this server)',
      marzbanInplaceDesc: 'Marzban is installed here and PasarGuard is NOT. Directories are renamed in-place.',
      marzbanFresh: 'Fresh PasarGuard install',
      marzbanFreshDesc: 'PasarGuard already installed, or you will upload a Marzban backup / use another server.',
      suggested: 'Suggested',
      alternative: 'Alternative',
    },
    step2: {
      h2: 'Source Database',
      desc: 'What database does your current panel use?',
      marzbanH2: 'Marzban Backup',
      marzbanDesc: 'Upload your Marzban backup — source database type is detected automatically.',
      marzbanDetectedLabel: 'Detected Marzban source database',
      marzbanDetectedOk: 'Detected from backup or live server data',
      marzbanDetectedWait: 'Upload backup to detect source database type',
      password: 'Database password',
      passwordPh: 'MySQL / MariaDB / PostgreSQL password',
      uploadH3: 'Or upload a backup',
      uploadDesc: 'zip, sql, or sqlite — Marzban backup, x-ui.db, MySQL dump',
      uploadDrag: 'Drag file here or',
      uploadSelect: 'browse',
      back: '← Back',
      next: 'Continue →',
      remnawaveUrl: 'Remnawave Panel URL',
      remnawaveUrlPh: 'https://panel.example.com',
      remnawaveToken: 'Remnawave API Token',
      remnawaveTokenPh: 'Bearer token from Remnawave dashboard',
    },
    step3: {
      h2: 'PasarGuard Target Database',
      desc: 'Which database should PasarGuard use?',
      marzbanH2: 'PasarGuard Install Database',
      marzbanDesc: 'Which database did you choose when installing PasarGuard on this server?',
      crossDbWarning: 'Cross-DB: {source} → {target}. Two-phase engine (head→head copy).',
      password: 'Target database password',
      passwordPh: 'New or existing password',
      pgMissing: 'PasarGuard is NOT installed',
      pgMissingDesc: 'Install PasarGuard manually on this server first. The installer needs interactive customization (domain, SSL, database, etc.) — this wizard cannot do that for you.',
      pgInstallHint: 'Run this on your server (choose database during install):',
      recheckPg: 'I installed it — Recheck',
      back: '← Back',
      next: 'Continue →',
    },
    step4: {
      h2: 'Review & Confirm',
      desc: 'Check the summary before starting.',
      redirect: 'Install redirect server to keep old 3x-ui subscription links working (recommended)',
      start: '🚀 Start Migration',
      back: '← Back',
      summary: {
        source: 'Source panel',
        sourceDb: 'Source database',
        targetDb: 'Target database',
        links: 'Subscription links',
        backup: 'Backup file',
        server: 'From server',
        pgInstallDb: 'PasarGuard install DB',
      },
    },
    step5: {
      h2: 'Migrating...',
      preparing: 'Preparing...',
    },
    step6: {
      success: 'Migration completed!',
      successLinks: 'All data and subscription links were preserved.',
      successRedirect: 'Data migrated. Old 3x-ui links work via redirect server.',
      successChanged: 'Data migrated. Inform users about new subscription links.',
      openPanel: 'Open PasarGuard Panel',
      error: 'Migration failed',
      retry: 'Try again',
    },
    support: { full: 'Full', partial: 'Partial', experimental: 'Experimental', db_only: 'DB only' },
    sub: { native: '✓ Links preserved', redirect: '✓ Links preserved via redirect', changed: '⚠ Links will change' },
    footer: { docs: 'PasarGuard Docs', github: 'GitHub' },
    uploading: 'Uploading',
    uploaded: 'uploaded',
    uploadErr: 'Upload error',
    detected: 'Detected',
    dbCred: {
      sourceHint: 'Open your Marzban .env on the server, copy DB values, and paste them below.',
      targetHint: 'Open PasarGuard .env with nano, copy DB_USER / DB_NAME / DB_PASSWORD, and enter them here.',
      sourceCmd: 'nano /opt/marzban/.env',
      user: 'DB user',
      dbName: 'DB name',
      host: 'DB host',
      port: 'DB port',
      password: 'DB password',
    },
    upload: {
      inventoryTitle: 'Backup contents',
      modeZip: 'Full ZIP (recommended)',
      modeSeparate: 'Separate files',
      required: 'Required',
      optional: 'Optional',
      browse: 'Choose file',
      allReady: 'All required files received — you can continue',
      waitingFiles: 'Waiting for required files',
      missing: 'Not uploaded yet',
      viaZip: 'from ZIP',
      fullZip: 'Full ZIP backup',
      separateFiles: 'Separate uploaded files',
      fileCount: 'Files',
      extractRoot: 'Data folder in zip',
      envMapping: 'Marzban → PasarGuard mapping',
      backupOk: 'Backup complete',
      backupIncomplete: 'Backup incomplete',
      pwdFromEnv: 'Will use password from backup .env',
      truncated: '…and more files (list truncated)',
      colType: 'Type',
      colPath: 'Path in zip',
      colSize: 'Size',
      colPgPath: 'PasarGuard path',
      cat: {
        database_sqlite: 'SQLite DB',
        database_sql: 'SQL dump',
        config_env: '.env',
        config_compose: 'docker-compose',
        config_xray: 'xray config',
        ssl_certs: 'SSL certs',
        templates: 'templates',
        other: 'other',
      },
    },
    block: {
      noRoot: 'Root access is required — run wizard as root',
      noDocker: 'Docker must be running',
      noPanel: 'Select a source panel',
      detectSourceDb: 'Upload Marzban backup — source DB will be detected automatically',
      prereqFailed: 'Fix all required prerequisites above before continuing',
      noSourceDb: 'Select source database type',
      selectDetectedDb: 'Confirm source database type (detected from backup)',
      noTargetDb: 'Select target database type',
      sourcePassword: 'Enter all source database credentials',
      targetPassword: 'Enter all target database credentials',
      sourceCredsIncomplete: 'Fill DB user, name, and password for source',
      targetCredsIncomplete: 'Fill DB user, name, and password for target',
      pasarguardMissing: 'Install PasarGuard manually, then click Recheck',
      marzbanBackup: 'Upload Marzban backup or use server with Marzban data',
      backupIncomplete: 'Backup zip is missing required files — see list below',
      dbMismatch: 'Selected DB type does not match backup contents',
      uploadsIncomplete: 'Upload all required files listed below',
      xuiDb: 'Upload x-ui.db or install 3x-ui on this server',
      remnawaveCreds: 'Enter Remnawave URL and API token',
      validationFailed: 'Pre-migration validation failed',
    },
  },
  fa: {
    title: 'PG-Migrator',
    subtitle: 'مهاجرت به PasarGuard',
    steps: ['پیش‌نیازها', 'پنل مبدأ', 'دیتابیس مبدأ', 'دیتابیس مقصد', 'تأیید', 'مهاجرت', 'نتیجه'],
    step0: {
      h2: 'قبل از شروع',
      desc: 'بخوانید چه چیزهایی باید قبل از مهاجرت روی سرور نصب باشد.',
      info: 'این ویزارد روی سرور Ubuntu با دسترسی root اجرا می‌شود. نیازمندی‌ها بعد از انتخاب پنل مبدأ نمایش داده می‌شوند.',
      checks: [
        ['🖥️', 'سرور Ubuntu', 'ویزارد روی پورت ۷۰۰۰'],
        ['🔑', 'دسترسی root', 'برای تغییر .env و Docker'],
        ['💾', 'بکاپ', 'قبل از مهاجرت حتماً بکاپ بگیرید'],
      ],
      start: 'ادامه ←',
      pasarguardCheck: 'PasarGuard روی سرور',
      pasarguardYes: 'نصب شده',
      pasarguardNo: 'نصب نیست — در صورت نیاز قبل از مهاجرت دستی نصب کنید',
      marzbanCheck: 'Marzban روی سرور',
      marzbanYes: 'نصب شده',
      marzbanNo: 'نصب نیست',
      dockerCheck: 'Docker',
      dockerYes: 'در حال اجرا',
      dockerNo: 'اجرا نمی‌شود',
      checking: 'در حال بررسی سرور...',
      checkingDetail: 'شناسایی PasarGuard، Marzban و Docker',
    },
    step1: {
      h2: 'انتخاب پنل مبدأ',
      desc: 'از کدام پنل می‌خواهید مهاجرت کنید؟',
      back: '→ بازگشت',
      next: 'ادامه ←',
      prereqTitle: 'چه چیزهایی باید نصب باشد:',
      uploadHint: 'چیزی کم است؟ در مرحله بعد بکاپ آپلود کنید.',
      marzbanModeTitle: 'روش مهاجرت مرزبان',
      marzbanModeDesc: 'بر اساس وضعیت سرور یکی از دو روش رسمی PasarGuard را انتخاب کنید.',
      marzbanInplace: 'درجا (مرزبان روی همین سرور)',
      marzbanInplaceDesc: 'مرزبان نصب است و PasarGuard نصب نیست. پوشه‌ها درجا تغییر نام می‌یابند.',
      marzbanFresh: 'نصب تازه PasarGuard',
      marzbanFreshDesc: 'PasarGuard از قبل نصب است، یا بکاپ مرزبان آپلود می‌کنید / سرور دیگر.',
      suggested: 'پیشنهادی',
      alternative: 'جایگزین',
    },
    step2: {
      h2: 'دیتابیس مبدأ',
      desc: 'پنل فعلی از چه دیتابیسی استفاده می‌کند؟',
      marzbanH2: 'بکاپ Marzban',
      marzbanDesc: 'بکاپ Marzban را آپلود کنید — نوع دیتابیس مبدأ خودکار تشخیص داده می‌شود.',
      marzbanDetectedLabel: 'دیتابیس مبدأ Marzban (تشخیص خودکار)',
      marzbanDetectedOk: 'از بکاپ یا داده زنده سرور تشخیص داده شد',
      marzbanDetectedWait: 'بکاپ آپلود کنید تا نوع دیتابیس مبدأ تشخیص داده شود',
      password: 'رمز دیتابیس',
      passwordPh: 'رمز MySQL / MariaDB / PostgreSQL',
      uploadH3: 'یا فایل بکاپ آپلود کنید',
      uploadDesc: 'zip، sql یا sqlite',
      uploadDrag: 'فایل را بکشید یا',
      uploadSelect: 'انتخاب کنید',
      back: '→ بازگشت',
      next: 'ادامه ←',
      remnawaveUrl: 'آدرس پنل Remnawave',
      remnawaveUrlPh: 'https://panel.example.com',
      remnawaveToken: 'توکن API Remnawave',
      remnawaveTokenPh: 'توکن از داشبورد Remnawave',
    },
    step3: {
      h2: 'دیتابیس مقصد PasarGuard',
      desc: 'PasarGuard روی کدام دیتابیس اجرا شود؟',
      marzbanH2: 'دیتابیس نصب PasarGuard',
      marzbanDesc: 'هنگام نصب PasarGuard روی این سرور، کدام دیتابیس را انتخاب کردید؟',
      crossDbWarning: 'مهاجرت DB: {source} → {target}. موتور دو‌فازی (کپی head→head).',
      password: 'رمز دیتابیس مقصد',
      passwordPh: 'رمز جدید یا موجود',
      pgMissing: 'PasarGuard نصب نیست',
      pgMissingDesc: 'ابتدا PasarGuard را دستی روی این سرور نصب کنید. نصب‌کننده نیاز به شخصی‌سازی تعاملی دارد (دامنه، SSL، دیتابیس و...) — ویزارد نمی‌تواند این کار را انجام دهد.',
      pgInstallHint: 'این دستور را روی سرور اجرا کنید (دیتابیس را در نصب انتخاب کنید):',
      recheckPg: 'نصب کردم — بررسی مجدد',
      back: '→ بازگشت',
      next: 'ادامه ←',
    },
    step4: {
      h2: 'تأیید نهایی',
      desc: 'خلاصه را بررسی کنید.',
      redirect: 'نصب redirect server برای حفظ لینک‌های قدیمی 3x-ui (توصیه‌شده)',
      start: '🚀 شروع مهاجرت',
      back: '→ بازگشت',
      summary: {
        source: 'پنل مبدأ',
        sourceDb: 'دیتابیس مبدأ',
        targetDb: 'دیتابیس مقصد',
        links: 'لینک اشتراک',
        backup: 'فایل بکاپ',
        server: 'از سرور',
        pgInstallDb: 'دیتابیس نصب PasarGuard',
      },
    },
    step5: { h2: 'در حال مهاجرت...', preparing: 'آماده‌سازی...' },
    step6: {
      success: 'مهاجرت موفق!',
      successLinks: 'داده‌ها و لینک اشتراک حفظ شدند.',
      successRedirect: 'داده‌ها منتقل شد. لینک‌های قدیمی 3x-ui با redirect کار می‌کنند.',
      successChanged: 'داده‌ها منتقل شد. لینک‌های جدید را به کاربران اطلاع دهید.',
      openPanel: 'ورود به پنل PasarGuard',
      error: 'خطا در مهاجرت',
      retry: 'تلاش مجدد',
    },
    support: { full: 'کامل', partial: 'جزئی', experimental: 'آزمایشی', db_only: 'فقط DB' },
    sub: { native: '✓ لینک‌ها حفظ می‌شوند', redirect: '✓ لینک‌ها با redirect حفظ می‌شوند', changed: '⚠ لینک‌ها تغییر می‌کنند' },
    footer: { docs: 'مستندات', github: 'GitHub' },
    uploading: 'در حال آپلود',
    uploaded: 'آپلود شد',
    uploadErr: 'خطا',
    detected: 'تشخیص',
    dbCred: {
      sourceHint: 'فایل .env مرزبان را با nano باز کنید، مقادیر دیتابیس را کپی و در فیلدهای زیر وارد کنید.',
      targetHint: 'فایل .env پاسارگارد را با nano باز کنید (DB_USER / DB_NAME / DB_PASSWORD) و مقادیر را دستی وارد کنید.',
      sourceCmd: 'nano /opt/marzban/.env',
      user: 'کاربر DB',
      dbName: 'نام DB',
      host: 'هاست DB',
      port: 'پورت DB',
      password: 'رمز DB',
    },
    upload: {
      inventoryTitle: 'محتوای بکاپ',
      modeZip: 'ZIP کامل (پیشنهادی)',
      modeSeparate: 'فایل‌های جدا',
      required: 'اجباری',
      optional: 'اختیاری',
      browse: 'انتخاب فایل',
      allReady: 'همه فایل‌های لازم دریافت شد — می‌توانید ادامه دهید',
      waitingFiles: 'در انتظار فایل‌های اجباری',
      missing: 'هنوز آپلود نشده',
      viaZip: 'از ZIP',
      fullZip: 'بکاپ ZIP کامل',
      separateFiles: 'فایل‌های جداگانه',
      fileCount: 'فایل',
      extractRoot: 'پوشه داده در zip',
      envMapping: 'نگاشت Marzban → PasarGuard',
      backupOk: 'بکاپ کامل است',
      backupIncomplete: 'بکاپ ناقص است',
      pwdFromEnv: 'رمز از .env بکاپ خوانده می‌شود',
      truncated: '…فایل‌های بیشتر (لیست کوتاه شده)',
      colType: 'نوع',
      colPath: 'مسیر در zip',
      colSize: 'حجم',
      colPgPath: 'مسیر PasarGuard',
      cat: {
        database_sqlite: 'دیتابیس SQLite',
        database_sql: 'فایل SQL',
        config_env: '.env',
        config_compose: 'docker-compose',
        config_xray: 'تنظیم xray',
        ssl_certs: 'گواهی SSL',
        templates: 'قالب‌ها',
        other: 'سایر',
      },
    },
    block: {
      noRoot: 'دسترسی root لازم است',
      noDocker: 'Docker باید در حال اجرا باشد',
      noPanel: 'پنل مبدأ را انتخاب کنید',
      detectSourceDb: 'بکاپ Marzban آپلود کنید — نوع DB مبدأ خودکار تشخیص داده می‌شود',
      prereqFailed: 'ابتدا همه پیش‌نیازهای اجباری را برطرف کنید',
      noSourceDb: 'نوع دیتابیس مبدأ را انتخاب کنید',
      selectDetectedDb: 'نوع دیتابیس را تأیید کنید (از بکاپ شناسایی شد)',
      noTargetDb: 'نوع دیتابیس مقصد را انتخاب کنید',
      sourcePassword: 'اطلاعات کامل دیتابیس مبدأ را وارد کنید',
      targetPassword: 'اطلاعات کامل دیتابیس مقصد را وارد کنید',
      sourceCredsIncomplete: 'کاربر، نام و رمز دیتابیس مبدأ را پر کنید',
      targetCredsIncomplete: 'کاربر، نام و رمز دیتابیس مقصد را پر کنید',
      pasarguardMissing: 'PasarGuard را دستی نصب کنید و بررسی مجدد بزنید',
      marzbanBackup: 'بکاپ Marzban آپلود کنید یا Marzban روی سرور باشد',
      backupIncomplete: 'فایل zip فاقد فایل‌های لازم است — لیست زیر را ببینید',
      dbMismatch: 'نوع دیتابیس انتخابی با محتوای بکاپ مطابقت ندارد',
      uploadsIncomplete: 'همه فایل‌های اجباری زیر را آپلود کنید',
      xuiDb: 'x-ui.db را آپلود کنید',
      remnawaveCreds: 'URL و Token رمناوی را وارد کنید',
      validationFailed: 'اعتبارسنجی پیش از مهاجرت ناموفق بود',
    },
  },
  ru: {
    title: 'PG-Migrator',
    subtitle: 'Миграция в PasarGuard',
    steps: ['Условия', 'Панель', 'БД источник', 'БД цель', 'Проверка', 'Миграция', 'Результат'],
    step0: {
      h2: 'Перед началом',
      desc: 'Узнайте, что должно быть установлено ДО миграции.',
      info: 'Мастер работает на Ubuntu с root. Требования зависят от исходной панели.',
      checks: [
        ['🖥️', 'Сервер Ubuntu', 'Порт 7000'],
        ['🔑', 'Root доступ', 'Для .env и Docker'],
        ['💾', 'Резервная копия', 'Сделайте бэкап'],
      ],
      start: 'Далее →',
      pasarguardCheck: 'PasarGuard на сервере',
      pasarguardYes: 'Установлен',
      pasarguardNo: 'Не установлен — установите вручную перед миграцией (если требуется)',
      marzbanCheck: 'Marzban на сервере',
      marzbanYes: 'Установлен',
      marzbanNo: 'Не установлен',
      dockerCheck: 'Docker',
      dockerYes: 'Работает',
      dockerNo: 'Не запущен',
      checking: 'Проверка сервера...',
      checkingDetail: 'Определение PasarGuard, Marzban, Docker',
    },
    step1: {
      h2: 'Исходная панель',
      desc: 'С какой панели мигрируете?',
      back: '← Назад',
      next: 'Далее →',
      prereqTitle: 'Что должно быть установлено:',
      uploadHint: 'Нет данных? Загрузите копию на следующем шаге.',
      marzbanModeTitle: 'Метод миграции Marzban',
      marzbanModeDesc: 'Выберите метод по состоянию сервера (официальная документация PasarGuard).',
      marzbanInplace: 'На месте (Marzban на этом сервере)',
      marzbanInplaceDesc: 'Marzban установлен, PasarGuard НЕТ. Каталоги переименовываются на месте.',
      marzbanFresh: 'Чистая установка PasarGuard',
      marzbanFreshDesc: 'PasarGuard уже установлен, или загрузите копию Marzban / другой сервер.',
      suggested: 'Рекомендуется',
      alternative: 'Альтернатива',
    },
    step2: {
      h2: 'База данных источника',
      desc: 'Какая БД у текущей панели?',
      marzbanH2: 'Копия Marzban',
      marzbanDesc: 'Загрузите копию Marzban — тип БД источника определится автоматически.',
      marzbanDetectedLabel: 'Определённая БД источника Marzban',
      marzbanDetectedOk: 'Определено из копии или данных на сервере',
      marzbanDetectedWait: 'Загрузите копию для определения типа БД источника',
      password: 'Пароль БД',
      passwordPh: 'Пароль MySQL / MariaDB / PostgreSQL',
      uploadH3: 'Или загрузите копию',
      uploadDesc: 'zip, sql или sqlite',
      uploadDrag: 'Перетащите файл или',
      uploadSelect: 'выберите',
      back: '← Назад',
      next: 'Далее →',
      remnawaveUrl: 'URL панели Remnawave',
      remnawaveUrlPh: 'https://panel.example.com',
      remnawaveToken: 'API Token Remnawave',
      remnawaveTokenPh: 'Токен из дашборда Remnawave',
    },
    step3: {
      h2: 'Целевая БД PasarGuard',
      desc: 'Какую БД использовать для PasarGuard?',
      marzbanH2: 'БД установки PasarGuard',
      marzbanDesc: 'Какую БД вы выбрали при установке PasarGuard на этом сервере?',
      crossDbWarning: 'Миграция БД: {source} → {target}. Двухфазный движок (head→head).',
      password: 'Пароль целевой БД',
      passwordPh: 'Новый или существующий пароль',
      pgMissing: 'PasarGuard НЕ установлен',
      pgMissingDesc: 'Сначала установите PasarGuard вручную на этом сервере. Установщик требует интерактивной настройки (домен, SSL, БД и т.д.) — мастер не может сделать это за вас.',
      pgInstallHint: 'Выполните на сервере (выберите БД при установке):',
      recheckPg: 'Установил — Проверить снова',
      back: '← Назад',
      next: 'Далее →',
    },
    step4: {
      h2: 'Подтверждение',
      desc: 'Проверьте сводку.',
      redirect: 'Установить redirect server для старых ссылок 3x-ui (рекомендуется)',
      start: '🚀 Начать миграцию',
      back: '← Назад',
      summary: {
        source: 'Исходная панель',
        sourceDb: 'БД источника',
        targetDb: 'Целевая БД',
        links: 'Ссылки подписки',
        backup: 'Файл копии',
        server: 'С сервера',
        pgInstallDb: 'БД установки PasarGuard',
      },
    },
    step5: { h2: 'Миграция...', preparing: 'Подготовка...' },
    step6: {
      success: 'Миграция завершена!',
      successLinks: 'Данные и ссылки сохранены.',
      successRedirect: 'Данные перенесены. Старые ссылки 3x-ui работают через redirect.',
      successChanged: 'Данные перенесены. Сообщите пользователям о новых ссылках.',
      openPanel: 'Открыть PasarGuard',
      error: 'Ошибка миграции',
      retry: 'Повторить',
    },
    support: { full: 'Полная', partial: 'Частичная', experimental: 'Эксперимент', db_only: 'Только БД' },
    sub: { native: '✓ Ссылки сохранены', redirect: '✓ Ссылки через redirect', changed: '⚠ Ссылки изменятся' },
    footer: { docs: 'Документация', github: 'GitHub' },
    uploading: 'Загрузка',
    uploaded: 'загружено',
    uploadErr: 'Ошибка',
    detected: 'Определено',
    dbCred: {
      sourceHint: 'Откройте .env Marzban через nano, скопируйте данные БД и введите ниже.',
      targetHint: 'Откройте .env PasarGuard (nano), скопируйте DB_USER / DB_NAME / DB_PASSWORD и введите вручную.',
      sourceCmd: 'nano /opt/marzban/.env',
      user: 'Пользователь БД',
      dbName: 'Имя БД',
      host: 'Хост БД',
      port: 'Порт БД',
      password: 'Пароль БД',
    },
    upload: {
      inventoryTitle: 'Содержимое копии',
      modeZip: 'Полный ZIP (рекомендуется)',
      modeSeparate: 'Отдельные файлы',
      required: 'Обязательно',
      optional: 'Опционально',
      browse: 'Выбрать файл',
      allReady: 'Все обязательные файлы получены — можно продолжить',
      waitingFiles: 'Ожидание обязательных файлов',
      missing: 'Ещё не загружен',
      viaZip: 'из ZIP',
      fullZip: 'Полный ZIP',
      separateFiles: 'Отдельные файлы',
      fileCount: 'Файлов',
      extractRoot: 'Папка данных в zip',
      envMapping: 'Соответствие Marzban → PasarGuard',
      backupOk: 'Копия полная',
      backupIncomplete: 'Копия неполная',
      pwdFromEnv: 'Пароль будет взят из .env копии',
      truncated: '…и другие файлы (список сокращён)',
      colType: 'Тип',
      colPath: 'Путь в zip',
      colSize: 'Размер',
      colPgPath: 'Путь PasarGuard',
      cat: {
        database_sqlite: 'SQLite БД',
        database_sql: 'SQL дамп',
        config_env: '.env',
        config_compose: 'docker-compose',
        config_xray: 'xray config',
        ssl_certs: 'SSL сертификаты',
        templates: 'шаблоны',
        other: 'прочее',
      },
    },
    block: {
      noRoot: 'Требуется root доступ',
      noDocker: 'Docker должен работать',
      noPanel: 'Выберите исходную панель',
      detectSourceDb: 'Загрузите копию Marzban — БД источника определится автоматически',
      prereqFailed: 'Исправьте все обязательные условия выше',
      noSourceDb: 'Выберите БД источника',
      selectDetectedDb: 'Подтвердите тип БД (определён из копии)',
      noTargetDb: 'Выберите целевую БД',
      sourcePassword: 'Введите все данные БД источника',
      targetPassword: 'Введите все данные целевой БД',
      sourceCredsIncomplete: 'Заполните пользователя, имя и пароль БД источника',
      targetCredsIncomplete: 'Заполните пользователя, имя и пароль целевой БД',
      pasarguardMissing: 'Установите PasarGuard вручную и нажмите Проверить',
      marzbanBackup: 'Загрузите копию Marzban или используйте сервер с Marzban',
      backupIncomplete: 'В zip нет нужных файлов — см. список ниже',
      dbMismatch: 'Выбранный тип БД не совпадает с содержимым копии',
      uploadsIncomplete: 'Загрузите все обязательные файлы ниже',
      xuiDb: 'Загрузите x-ui.db',
      remnawaveCreds: 'Введите URL и токен Remnawave',
      validationFailed: 'Проверка перед миграцией не пройдена',
    },
  },
};

function t(key) {
  const lang = state.lang || 'en';
  const parts = key.split('.');
  let v = I18N[lang];
  for (const p of parts) v = v?.[p];
  return v ?? I18N.en[key] ?? key;
}

function tr(obj, lang) {
  if (!obj) return '';
  if (typeof obj === 'string') return obj;
  if (Array.isArray(obj)) return obj;
  return obj[lang] || obj.en || '';
}

function setLang(lang) {
  state.lang = lang;
  localStorage.setItem('pg-migrator-lang', lang);
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === 'fa' ? 'rtl' : 'ltr';
  document.querySelectorAll('.lang-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.lang === lang);
  });
  applyI18n();
  renderGlobalChecks();
  if (state.selectedPanel) renderPanelPrereqs(state.selectedPanel.id);
  if (state.selectedPanel?.id === 'marzban' && typeof renderMarzbanModes === 'function') renderMarzbanModes();
  if (state.currentStep === 1 && state.panels.length) renderPanels();
}

function applyI18n() {
  const map = {
    subtitle: '.subtitle',
    'step0.h2': '#step0 h2', 'step0.desc': '#step0 .desc', 'step0.info': '#step0 .info-box p',
    'step0.start': '#btnStep0',
    'step1.h2': '#step1 h2', 'step1.desc': '#step1 .desc', 'step1.back': '#step1 .btn-ghost',
    'step1.next': '#btnStep1',
    'step2.h2': '#step2 h2', 'step2.desc': '#step2 .desc',
    'step2.uploadH3': '.upload-section h3', 'step2.uploadDesc': '.upload-section .desc-sm',
    'step2.back': '#step2 .btn-ghost', 'step2.next': '#btnStep2',
    'step3.h2': '#step3 h2', 'step3.desc': '#step3 .desc',
    'step3.pgMissing': '#installPgSection h4', 'step3.pgMissingDesc': '#installPgSection > p:first-of-type',
    'step3.pgInstallHint': '#installPgHint',
    'step3.recheckPg': '#btnRecheckPg', 'step3.back': '#step3 .btn-ghost', 'step3.next': '#btnStep3',
    'step4.h2': '#step4 h2', 'step4.desc': '#step4 .desc', 'step4.start': '#step4 .btn-lg',
    'step4.back': '#step4 .btn-ghost',
    'step5.h2': '#step5 h2',
    'step6.openPanel': '#panelLink', 'step6.retry': '#resultError .btn-secondary',
  };
  for (const [k, sel] of Object.entries(map)) {
    const el = document.querySelector(sel);
    if (el) el.textContent = t(k);
  }
  document.querySelector('#redirectOption span') && (document.querySelector('#redirectOption span').textContent = t('step4.redirect'));
  document.querySelector('#step6 h2.success-title') && (document.querySelector('#step6 .result-card.success h2').textContent = t('step6.success'));
  document.querySelector('#resultError h2') && (document.querySelector('#resultError h2').textContent = t('step6.error'));
  const credLabels = [
    ['lblSourceDbUser', 'dbCred.user'], ['lblSourceDbName', 'dbCred.dbName'], ['lblSourceDbHost', 'dbCred.host'],
    ['lblSourceDbPort', 'dbCred.port'], ['lblSourceDbPassword', 'dbCred.password'],
    ['lblTargetDbUser', 'dbCred.user'], ['lblTargetDbName', 'dbCred.dbName'], ['lblTargetDbHost', 'dbCred.host'],
    ['lblTargetDbPort', 'dbCred.port'], ['lblTargetDbPassword', 'dbCred.password'],
  ];
  credLabels.forEach(([id, key]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = t(key);
  });
  const srcHint = document.getElementById('sourceCredHint');
  const tgtHint = document.getElementById('targetCredHint');
  if (srcHint) srcHint.textContent = t('dbCred.sourceHint');
  if (tgtHint) tgtHint.textContent = t('dbCred.targetHint');
  const srcCmd = document.getElementById('sourceEnvCmd');
  if (srcCmd) srcCmd.textContent = t('dbCred.sourceCmd');
  document.title = `${t('title')} — ${t('subtitle')}`;
  renderSteps();
  renderGlobalChecks();
  updateStepButtons();
}

function renderSteps() {
  const steps = t('steps');
  if (!Array.isArray(steps)) return;
  document.querySelectorAll('.step').forEach((el, i) => {
    const label = el.querySelector('.step-label');
    const num = el.querySelector('.step-num');
    if (label) label.textContent = steps[i];
    if (num) num.textContent = state.lang === 'fa' ? String(i) : String(i);
  });
}

function renderGlobalChecks() {
  const checks = t('step0.checks');
  if (!Array.isArray(checks)) return;
  let html = checks.map(([icon, title, detail]) => `
    <div class="check-item"><span class="check-icon">${icon}</span><div><div>${title}</div><div class="check-detail">${detail}</div></div></div>`).join('');

  const sys = state.systemCheck;
  if (sys) {
    const pgIcon = sys.pasarguard ? '✅' : '❌';
    const pgDetail = sys.pasarguard
      ? `${t('step0.pasarguardYes')}${sys.pasarguard_path ? ` — ${sys.pasarguard_path}` : ''}${sys.pasarguard_db ? ` (${sys.pasarguard_db})` : ''}`
      : t('step0.pasarguardNo');
    html += `<div class="check-item check-live"><span class="check-icon">${pgIcon}</span><div><div><strong>${t('step0.pasarguardCheck')}</strong></div><div class="check-detail">${pgDetail}</div></div></div>`;

    const mzIcon = sys.marzban ? '✅' : '⚠️';
    const mzDetail = sys.marzban
      ? `${t('step0.marzbanYes')}${sys.marzban_path ? ` — ${sys.marzban_path}` : ''}${sys.marzban_db ? ` (${sys.marzban_db})` : ''}`
      : t('step0.marzbanNo');
    html += `<div class="check-item check-live"><span class="check-icon">${mzIcon}</span><div><div><strong>${t('step0.marzbanCheck')}</strong></div><div class="check-detail">${mzDetail}</div></div></div>`;

    const dkIcon = sys.docker ? '✅' : '❌';
    const dkDetail = sys.docker ? t('step0.dockerYes') : t('step0.dockerNo');
    html += `<div class="check-item check-live"><span class="check-icon">${dkIcon}</span><div><div><strong>${t('step0.dockerCheck')}</strong></div><div class="check-detail">${dkDetail}</div></div></div>`;
  } else {
    html += `<div class="check-item"><span class="check-icon">⏳</span><div><div>${t('step0.checking')}</div><div class="check-detail">${t('step0.checkingDetail')}</div></div></div>`;
  }

  document.getElementById('globalChecks').innerHTML = html;
}
