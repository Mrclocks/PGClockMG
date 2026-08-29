/* PGClockMG Backup panel client */

const I18N = {
  fa: {
    logout: "خروج",
    tabDash: "داشبورد",
    tabList: "بکاپ‌ها",
    tabSettings: "تنظیمات",
    authSetupTitle: "رمز پنل بکاپ",
    authSetupDesc: "اولین ورود: یک رمز قوی برای پنل بکاپ بگذارید.",
    authLoginTitle: "ورود به پنل بکاپ",
    authLoginDesc: "با رمز پنل بکاپ وارد شوید.",
    lblPassword: "رمز عبور",
    lblPasswordConfirm: "تکرار رمز",
    authPolicy: "حداقل ۱۲ کاراکتر، شامل حرف بزرگ، کوچک، عدد و نماد.",
    btnSetup: "ذخیره و ورود",
    btnLogin: "ورود",
    dashH2: "پایش و بکاپ",
    dashDesc: "وضعیت PasarGuard و یک کلیک برای بکاپ کامل.",
    dashPgTitle: "وضعیت PasarGuard",
    btnBackupNow: "بکاپ کامل همین حالا",
    backupRunning: "در حال ساخت بکاپ…",
    backupDone: "بکاپ آماده شد",
    backupFail: "بکاپ ناموفق بود",
    listH2: "فایل‌های بکاپ",
    listDesc: "دانلود، ارسال به تلگرام، یا استریم به سرور مقصد.",
    emptyList: "هنوز بکاپی نیست.",
    download: "دانلود",
    sendTg: "تلگرام",
    sendStream: "استریم",
    remove: "حذف",
    setH2: "تنظیمات",
    setDesc: "زمان‌بندی، تلگرام، پروکسی و استریم.",
    setSchedTitle: "زمان‌بندی خودکار",
    lblSchedEnabled: "بکاپ خودکار روزانه (UTC)",
    lblSchedHour: "ساعت",
    lblSchedMinute: "دقیقه",
    lblRetention: "تعداد نگه‌داری",
    lblSchedTelegram: "بعد از بکاپ خودکار به تلگرام هم بفرست",
    setTgTitle: "تلگرام",
    lblTgEnabled: "ارسال به تلگرام فعال باشد",
    lblTgToken: "Bot Token",
    lblTgChat: "Chat ID",
    lblTgCaption: "متن پیام",
    tgCaptionHint: "متغیرها: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgPreview: "پیش‌نمایش متن",
    btnTgTest: "تست اتصال",
    setProxyTitle: "پروکسی تلگرام",
    lblProxyEnabled: "از پروکسی استفاده کن",
    lblProxyType: "نوع",
    lblProxyHost: "هاست",
    lblProxyPort: "پورت",
    lblProxyUser: "کاربر",
    lblProxyPass: "رمز پروکسی",
    setStreamTitle: "مقصد استریم پیش‌فرض",
    lblStreamDest: "آدرس ویزارد مقصد",
    streamDestHint: "مثال: http://IP:7000 — روی مقصد از ویزارد «دریافت استریم» را بزنید.",
    setPassTitle: "تغییر رمز پنل",
    lblNewPass: "رمز جدید",
    lblNewPassConfirm: "تکرار رمز جدید",
    btnSaveSettings: "ذخیره تنظیمات",
    saved: "ذخیره شد",
    streamH2: "ارسال استریم",
    streamDesc: "بکاپ روی سرور می‌ماند؛ فقط به مقصد استریم می‌شود. مقصد باید آمادهٔ دریافت باشد.",
    lblStreamUrl: "آدرس ویزارد مقصد",
    lblStreamToken: "توکن دریافت",
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
  },
  en: {
    logout: "Logout",
    tabDash: "Dashboard",
    tabList: "Backups",
    tabSettings: "Settings",
    authSetupTitle: "Backup panel password",
    authSetupDesc: "First run: set a strong password for the backup panel.",
    authLoginTitle: "Backup panel login",
    authLoginDesc: "Sign in with your backup panel password.",
    lblPassword: "Password",
    lblPasswordConfirm: "Confirm password",
    authPolicy: "At least 12 chars, with upper, lower, digit, and symbol.",
    btnSetup: "Save & enter",
    btnLogin: "Sign in",
    dashH2: "Health & backup",
    dashDesc: "PasarGuard status and one-click full backup.",
    dashPgTitle: "PasarGuard status",
    btnBackupNow: "Create full backup now",
    backupRunning: "Creating backup…",
    backupDone: "Backup ready",
    backupFail: "Backup failed",
    listH2: "Backup files",
    listDesc: "Download, send to Telegram, or stream to a destination server.",
    emptyList: "No backups yet.",
    download: "Download",
    sendTg: "Telegram",
    sendStream: "Stream",
    remove: "Delete",
    setH2: "Settings",
    setDesc: "Schedule, Telegram, proxy, and stream defaults.",
    setSchedTitle: "Automatic schedule",
    lblSchedEnabled: "Daily automatic backup (UTC)",
    lblSchedHour: "Hour",
    lblSchedMinute: "Minute",
    lblRetention: "Keep last N",
    lblSchedTelegram: "Also send scheduled backups to Telegram",
    setTgTitle: "Telegram",
    lblTgEnabled: "Enable Telegram delivery",
    lblTgToken: "Bot Token",
    lblTgChat: "Chat ID",
    lblTgCaption: "Message text",
    tgCaptionHint: "Vars: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgPreview: "Preview text",
    btnTgTest: "Test connection",
    setProxyTitle: "Telegram proxy",
    lblProxyEnabled: "Use proxy",
    lblProxyType: "Type",
    lblProxyHost: "Host",
    lblProxyPort: "Port",
    lblProxyUser: "User",
    lblProxyPass: "Proxy password",
    setStreamTitle: "Default stream destination",
    lblStreamDest: "Destination wizard URL",
    streamDestHint: "Example: http://IP:7000 — on destination open wizard Receive Stream.",
    setPassTitle: "Change panel password",
    lblNewPass: "New password",
    lblNewPassConfirm: "Confirm new password",
    btnSaveSettings: "Save settings",
    saved: "Saved",
    streamH2: "Stream send",
    streamDesc: "File stays on this server; only streamed to destination. Destination must be listening.",
    lblStreamUrl: "Destination wizard URL",
    lblStreamToken: "Receive token",
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
  },
  ru: {
    logout: "Выход",
    tabDash: "Панель",
    tabList: "Бэкапы",
    tabSettings: "Настройки",
    authSetupTitle: "Пароль панели бэкапа",
    authSetupDesc: "Первый запуск: задайте надёжный пароль.",
    authLoginTitle: "Вход в панель бэкапа",
    authLoginDesc: "Войдите с паролем панели бэкапа.",
    lblPassword: "Пароль",
    lblPasswordConfirm: "Повтор пароля",
    authPolicy: "Минимум 12 символов: заглавные, строчные, цифра и спецсимвол.",
    btnSetup: "Сохранить и войти",
    btnLogin: "Войти",
    dashH2: "Мониторинг и бэкап",
    dashDesc: "Статус PasarGuard и полный бэкап в один клик.",
    dashPgTitle: "Статус PasarGuard",
    btnBackupNow: "Сделать полный бэкап",
    backupRunning: "Создание бэкапа…",
    backupDone: "Бэкап готов",
    backupFail: "Ошибка бэкапа",
    listH2: "Файлы бэкапов",
    listDesc: "Скачать, отправить в Telegram или стримом на сервер.",
    emptyList: "Бэкапов пока нет.",
    download: "Скачать",
    sendTg: "Telegram",
    sendStream: "Стрим",
    remove: "Удалить",
    setH2: "Настройки",
    setDesc: "Расписание, Telegram, прокси и стрим.",
    setSchedTitle: "Авторасписание",
    lblSchedEnabled: "Ежедневный бэкап (UTC)",
    lblSchedHour: "Час",
    lblSchedMinute: "Минута",
    lblRetention: "Хранить N",
    lblSchedTelegram: "Также слать в Telegram",
    setTgTitle: "Telegram",
    lblTgEnabled: "Включить Telegram",
    lblTgToken: "Bot Token",
    lblTgChat: "Chat ID",
    lblTgCaption: "Текст сообщения",
    tgCaptionHint: "Переменные: {date} {size} {db_type} {users} {nodes} {status} {filename} {parts}",
    btnTgPreview: "Превью",
    btnTgTest: "Тест",
    setProxyTitle: "Прокси Telegram",
    lblProxyEnabled: "Использовать прокси",
    lblProxyType: "Тип",
    lblProxyHost: "Хост",
    lblProxyPort: "Порт",
    lblProxyUser: "Пользователь",
    lblProxyPass: "Пароль прокси",
    setStreamTitle: "Назначение стрима",
    lblStreamDest: "URL мастера назначения",
    streamDestHint: "Пример: http://IP:7000",
    setPassTitle: "Смена пароля",
    lblNewPass: "Новый пароль",
    lblNewPassConfirm: "Повтор",
    btnSaveSettings: "Сохранить",
    saved: "Сохранено",
    streamH2: "Стрим",
    streamDesc: "Файл остаётся здесь; только стрим на назначение.",
    lblStreamUrl: "URL мастера",
    lblStreamToken: "Токен",
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
}

function applyI18n() {
  const map = [
    ["tabDash", "tabDash"], ["tabList", "tabList"], ["tabSettings", "tabSettings"],
    ["btnLogout", "logout"], ["lblPassword", "lblPassword"], ["lblPasswordConfirm", "lblPasswordConfirm"],
    ["authPolicy", "authPolicy"], ["dashH2", "dashH2"], ["dashDesc", "dashDesc"], ["dashPgTitle", "dashPgTitle"],
    ["btnBackupNow", "btnBackupNow"], ["listH2", "listH2"], ["listDesc", "listDesc"],
    ["setH2", "setH2"], ["setDesc", "setDesc"], ["setSchedTitle", "setSchedTitle"],
    ["lblSchedEnabled", "lblSchedEnabled"], ["lblSchedHour", "lblSchedHour"], ["lblSchedMinute", "lblSchedMinute"],
    ["lblRetention", "lblRetention"], ["lblSchedTelegram", "lblSchedTelegram"],
    ["setTgTitle", "setTgTitle"], ["lblTgEnabled", "lblTgEnabled"], ["lblTgToken", "lblTgToken"],
    ["lblTgChat", "lblTgChat"], ["lblTgCaption", "lblTgCaption"], ["tgCaptionHint", "tgCaptionHint"],
    ["btnTgPreview", "btnTgPreview"], ["btnTgTest", "btnTgTest"],
    ["setProxyTitle", "setProxyTitle"], ["lblProxyEnabled", "lblProxyEnabled"],
    ["lblProxyType", "lblProxyType"], ["lblProxyHost", "lblProxyHost"], ["lblProxyPort", "lblProxyPort"],
    ["lblProxyUser", "lblProxyUser"], ["lblProxyPass", "lblProxyPass"],
    ["setStreamTitle", "setStreamTitle"], ["lblStreamDest", "lblStreamDest"], ["streamDestHint", "streamDestHint"],
    ["setPassTitle", "setPassTitle"], ["lblNewPass", "lblNewPass"], ["lblNewPassConfirm", "lblNewPassConfirm"],
    ["btnSaveSettings", "btnSaveSettings"],
    ["streamH2", "streamH2"], ["streamDesc", "streamDesc"], ["lblStreamUrl", "lblStreamUrl"],
    ["lblStreamToken", "lblStreamToken"], ["btnStreamSend", "btnStreamSend"], ["btnStreamBack", "btnStreamBack"],
  ];
  for (const [id, key] of map) {
    const el = document.getElementById(id);
    if (el) el.textContent = t(key);
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
  if (ct.includes("application/json")) {
    data = await res.json();
  } else {
    data = await res.text();
  }
  if (!res.ok) {
    const detail = (data && data.detail) || data || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function showPanel(id) {
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  const el = document.getElementById(id);
  if (el) el.classList.add("active");
}

function showBackupTab(tab) {
  document.getElementById("backupTabs").classList.remove("hidden");
  document.getElementById("btnLogout").classList.remove("hidden");
  document.querySelectorAll("#backupTabs .step").forEach((s) => {
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
  if (n == null) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return (i === 0 ? String(Math.round(v)) : v.toFixed(1)) + " " + units[i];
}

async function boot() {
  setBackupLang(lang);
  const st = await api("/api/setup/status");
  document.getElementById("appVersion").textContent = "v" + (st.version || "3.3.0");
  setupMode = !st.password_set;
  document.getElementById("authConfirmWrap").classList.toggle("hidden", !setupMode);
  applyI18n();

  if (!setupMode) {
    // probe session
    try {
      await api("/api/dashboard");
      enterApp();
      return;
    } catch (_) {
      /* need login */
    }
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
      await api("/api/setup/password", {
        method: "POST",
        body: JSON.stringify({ password, password_confirm }),
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
  const specs = document.getElementById("dashPgSpecs");
  const sys = data.system || {};
  const installed = data.pasarguard_installed;
  document.getElementById("dashPgCard").className = installed ? "success-card" : "warning-card";
  specs.innerHTML = `
    <div class="specs-item"><span class="specs-label">PasarGuard</span><span class="specs-value">${installed ? "OK" : "—"}</span></div>
    <div class="specs-item"><span class="specs-label">${t("db")}</span><span class="specs-value">${sys.pasarguard_db || "—"}</span></div>
    <div class="specs-item"><span class="specs-label">Docker</span><span class="specs-value">${sys.docker ? "OK" : "—"}</span></div>
  `;

  const counts = (data.live_stats && data.live_stats.counts) || {};
  const keys = ["users", "nodes", "admins", "inbounds", "hosts", "groups"];
  document.getElementById("dashStats").innerHTML = keys.map((k) => `
    <div class="choice-card">
      <strong>${counts[k] == null ? "—" : counts[k]}</strong>
      <span>${t(k)}</span>
    </div>
  `).join("");

  const last = data.last_backup;
  const box = document.getElementById("dashLast");
  if (!last) {
    box.textContent = t("noLast");
  } else {
    box.innerHTML = `<strong>${t("lastBackup")}</strong><br>${last.filename || last.backup_id}<br>${t("size")}: ${humanSize(last.size_bytes)} · ${last.created_at || ""}`;
  }
}

async function createBackupNow() {
  const box = document.getElementById("backupProgress");
  const title = document.getElementById("backupProgressTitle");
  const logEl = document.getElementById("backupProgressLog");
  box.classList.remove("hidden");
  title.textContent = t("backupRunning");
  logEl.textContent = "";
  document.getElementById("btnBackupNow").disabled = true;
  try {
    const job = await api("/api/backups/create", { method: "POST", body: "{}" });
    await pollJob(job.job_id, title, logEl);
    await refreshDashboard();
    await refreshList();
  } catch (e) {
    title.textContent = t("backupFail") + ": " + e.message;
  } finally {
    document.getElementById("btnBackupNow").disabled = false;
  }
}

async function pollJob(jobId, title, logEl) {
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
    root.innerHTML = `<div class="info-box">${t("emptyList")}</div>`;
    return;
  }
  root.innerHTML = items.map((it) => {
    const m = it.manifest || {};
    const c = m.counts || {};
    return `<div class="backup-item">
      <div class="backup-item-head">
        <strong>${it.filename}</strong>
        <span>${humanSize(it.size_bytes)}</span>
      </div>
      <div class="backup-item-meta">
        ${it.mtime || ""} · ${m.db_type || "?"} · ${t("users")}: ${c.users ?? "—"} · ${t("nodes")}: ${c.nodes ?? "—"}
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
}

async function openStream(id) {
  document.getElementById("streamBackupId").value = id;
  const settings = await api("/api/settings");
  document.getElementById("streamUrl").value = (settings.stream && settings.stream.default_dest_url) || "";
  document.getElementById("streamToken").value = "";
  document.getElementById("streamMsg").classList.add("hidden");
  showBackupTab("stream");
}

async function sendStream() {
  const msg = document.getElementById("streamMsg");
  msg.classList.remove("hidden");
  msg.textContent = "…";
  try {
    const r = await api("/api/backups/stream/send", {
      method: "POST",
      body: JSON.stringify({
        backup_id: document.getElementById("streamBackupId").value,
        dest_url: document.getElementById("streamUrl").value,
        token: document.getElementById("streamToken").value,
      }),
    });
    msg.textContent = "OK · " + JSON.stringify(r.response || r);
  } catch (e) {
    msg.textContent = e.message;
  }
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
  if (np || npc) {
    await api("/api/password/change", {
      method: "POST",
      body: JSON.stringify({ password: np, password_confirm: npc }),
    });
    document.getElementById("newPassword").value = "";
    document.getElementById("newPasswordConfirm").value = "";
  }
  msg.textContent = t("saved");
  msg.classList.remove("hidden");
}

async function testTelegram() {
  try {
    await saveSettings();
    const r = await api("/api/telegram/test", { method: "POST", body: "{}" });
    alert("OK · @" + ((r.bot && r.bot.username) || "?"));
  } catch (e) {
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
