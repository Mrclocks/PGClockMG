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
        ['🐳', 'Docker', 'Required for Marzban / PasarGuard panels'],
        ['💾', 'Backup', 'Always backup before migrating'],
      ],
      start: 'Continue →',
    },
    step1: {
      h2: 'Select Source Panel',
      desc: 'Which panel are you migrating from?',
      back: '← Back',
      next: 'Continue →',
      prereqTitle: 'What must be installed:',
      uploadHint: 'Missing something? You can upload a backup in the next step.',
    },
    step2: {
      h2: 'Source Database',
      desc: 'What database does your current panel use?',
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
      password: 'Target database password',
      passwordPh: 'New or existing password',
      pgMissing: 'PasarGuard is NOT installed',
      pgMissingDesc: 'This migration type requires PasarGuard on this server first.',
      installPg: 'Install PasarGuard',
      installing: 'Installing PasarGuard — may take a few minutes...',
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
        ['🐳', 'Docker', 'برای پنل Marzban / PasarGuard'],
        ['💾', 'بکاپ', 'قبل از مهاجرت حتماً بکاپ بگیرید'],
      ],
      start: 'ادامه ←',
    },
    step1: {
      h2: 'انتخاب پنل مبدأ',
      desc: 'از کدام پنل می‌خواهید مهاجرت کنید؟',
      back: '→ بازگشت',
      next: 'ادامه ←',
      prereqTitle: 'چه چیزهایی باید نصب باشد:',
      uploadHint: 'چیزی کم است؟ در مرحله بعد بکاپ آپلود کنید.',
    },
    step2: {
      h2: 'دیتابیس مبدأ',
      desc: 'پنل فعلی از چه دیتابیسی استفاده می‌کند؟',
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
      password: 'رمز دیتابیس مقصد',
      passwordPh: 'رمز جدید یا موجود',
      pgMissing: 'PasarGuard نصب نیست',
      pgMissingDesc: 'این نوع مهاجرت نیاز به نصب قبلی PasarGuard دارد.',
      installPg: 'نصب PasarGuard',
      installing: 'در حال نصب PasarGuard...',
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
        ['🐳', 'Docker', 'Для Marzban / PasarGuard'],
        ['💾', 'Резервная копия', 'Сделайте бэкап'],
      ],
      start: 'Далее →',
    },
    step1: {
      h2: 'Исходная панель',
      desc: 'С какой панели мигрируете?',
      back: '← Назад',
      next: 'Далее →',
      prereqTitle: 'Что должно быть установлено:',
      uploadHint: 'Нет данных? Загрузите копию на следующем шаге.',
    },
    step2: {
      h2: 'База данных источника',
      desc: 'Какая БД у текущей панели?',
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
      password: 'Пароль целевой БД',
      passwordPh: 'Новый или существующий пароль',
      pgMissing: 'PasarGuard НЕ установлен',
      pgMissingDesc: 'Сначала установите PasarGuard на этом сервере.',
      installPg: 'Установить PasarGuard',
      installing: 'Установка PasarGuard...',
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
  if (state.selectedPanel) renderPanelPrereqs(state.selectedPanel);
  if (state.currentStep === 1 && state.panels.length) renderPanels();
}

function applyI18n() {
  const map = {
    subtitle: '.subtitle',
    'step0.h2': '#step0 h2', 'step0.desc': '#step0 .desc', 'step0.info': '#step0 .info-box p',
    'step0.start': '#step0 .btn-primary',
    'step1.h2': '#step1 h2', 'step1.desc': '#step1 .desc', 'step1.back': '#step1 .btn-ghost',
    'step1.next': '#btnStep1',
    'step2.h2': '#step2 h2', 'step2.desc': '#step2 .desc', 'step2.password': '#dbCredentials label',
    'step2.uploadH3': '.upload-section h3', 'step2.uploadDesc': '.upload-section .desc-sm',
    'step2.back': '#step2 .btn-ghost', 'step2.next': '#btnStep2',
    'step3.h2': '#step3 h2', 'step3.desc': '#step3 .desc', 'step3.password': '#targetCredentials label',
    'step3.pgMissing': '#installPgSection h4', 'step3.pgMissingDesc': '#installPgSection p',
    'step3.installPg': '#installPgSection .btn-secondary', 'step3.back': '#step3 .btn-ghost', 'step3.next': '#btnStep3',
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
  document.getElementById('sourcePassword').placeholder = t('step2.passwordPh');
  document.getElementById('targetPassword').placeholder = t('step3.passwordPh');
  document.title = `${t('title')} — ${t('subtitle')}`;
  renderSteps();
  renderGlobalChecks();
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
  document.getElementById('globalChecks').innerHTML = checks.map(([icon, title, detail]) => `
    <div class="check-item"><span class="check-icon">${icon}</span><div><div>${title}</div><div class="check-detail">${detail}</div></div></div>`).join('');
}
