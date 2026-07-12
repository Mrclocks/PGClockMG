/* PG-Migrator Wizard */

const state = {
  currentStep: 0,
  panels: [],
  selectedPanel: null,
  sourceDb: null,
  targetDb: null,
  sourcePassword: '',
  targetPassword: '',
  uploadId: null,
  uploadInfo: null,
  jobId: null,
  serverIp: '',
  detected: {},
};

const SUPPORT_LABELS = {
  full: { text: 'کامل', class: 'support-full' },
  partial: { text: 'جزئی', class: 'support-partial' },
  experimental: { text: 'آزمایشی', class: 'support-experimental' },
  db_only: { text: 'فقط DB', class: 'support-db_only' },
};

// ─── Init ───────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await loadInfo();
  renderGlobalChecks();
  setupUpload();
});

async function loadInfo() {
  try {
    const res = await fetch('/api/info');
    const data = await res.json();
    state.panels = data.panels;
    state.serverIp = data.server_ip;
    document.getElementById('serverIp').textContent = `${data.server_ip}:${data.web_port}`;
  } catch (e) {
    console.error('Failed to load info', e);
  }
}

function renderGlobalChecks() {
  const el = document.getElementById('globalChecks');
  el.innerHTML = `
    <div class="check-item"><span class="check-icon">🖥️</span><div><div>سرور Ubuntu</div><div class="check-detail">وب‌پنل روی پورت ۷۰۰۰ فعال است</div></div></div>
    <div class="check-item"><span class="check-icon">🔑</span><div><div>دسترسی root</div><div class="check-detail">برای تغییر .env و docker لازم است</div></div></div>
    <div class="check-item"><span class="check-icon">🐳</span><div><div>Docker</div><div class="check-detail">برای نصب و اجرای PasarGuard</div></div></div>
    <div class="check-item"><span class="check-icon">💾</span><div><div>بکاپ</div><div class="check-detail">قبل از مهاجرت حتماً بکاپ بگیرید</div></div></div>
  `;
}

// ─── Navigation ─────────────────────────────────────
function goStep(n) {
  if (n === 1) renderPanels();
  if (n === 2) renderSourceDbs();
  if (n === 3) renderTargetDbs();
  if (n === 4) renderSummary();

  state.currentStep = n;
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById(`step${n}`).classList.add('active');

  document.querySelectorAll('.step').forEach(s => {
    const sn = parseInt(s.dataset.step);
    s.classList.toggle('active', sn === n);
    s.classList.toggle('done', sn < n);
  });
}

// ─── Step 1: Panels ─────────────────────────────────
function renderPanels() {
  const grid = document.getElementById('panelGrid');
  grid.innerHTML = state.panels.map(p => {
    const sup = SUPPORT_LABELS[p.support_level] || SUPPORT_LABELS.partial;
    const subClass = p.subscription_preserved ? 'sub-yes' : 'sub-no';
    const subText = p.subscription_preserved ? '✓ لینک اشتراک حفظ می‌شود' : '⚠ لینک اشتراک تغییر می‌کند';
    return `
      <div class="panel-card" data-id="${p.id}" onclick="selectPanel('${p.id}')">
        <span class="support-badge ${sup.class}">${sup.text}</span>
        <div class="icon">${p.icon}</div>
        <h3>${p.name_fa}</h3>
        <p>${p.description_fa}</p>
        <div class="sub-preserve ${subClass}">${subText}</div>
      </div>`;
  }).join('');
}

async function selectPanel(id) {
  state.selectedPanel = state.panels.find(p => p.id === id);
  document.querySelectorAll('.panel-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.id === id);
  });

  const panel = state.selectedPanel;
  const warnEl = document.getElementById('panelWarnings');
  if (panel.warnings_fa?.length) {
    warnEl.classList.remove('hidden');
    warnEl.innerHTML = '<strong>⚠️ نکات مهم:</strong><ul>' +
      panel.warnings_fa.map(w => `<li style="margin:6px 0;font-size:0.85rem">${w}</li>`).join('') +
      '</ul>';
  } else {
    warnEl.classList.add('hidden');
  }

  const prereqEl = document.getElementById('panelPrereqs');
  prereqEl.classList.remove('hidden');
  prereqEl.innerHTML = '<div style="text-align:center;padding:12px;color:var(--text-dim)">در حال بررسی...</div>';

  try {
    const res = await fetch(`/api/prerequisites/${id}`);
    const data = await res.json();
    state.detected = data.detected || {};
    prereqEl.innerHTML = data.checks.map(c => `
      <div class="check-item">
        <span class="check-icon">${c.ok ? '✅' : (c.optional ? '⚠️' : '❌')}</span>
        <div>
          <div>${c.label_fa}</div>
          <div class="check-detail">${c.detail_fa}</div>
        </div>
      </div>`).join('');

    document.getElementById('btnStep1').disabled = !data.ok && !state.uploadId;
    if (!data.ok) {
      prereqEl.innerHTML += `<div class="info-box" style="margin-top:12px">💡 می‌توانید با آپلود فایل بکاپ در مرحله بعد ادامه دهید.</div>`;
      document.getElementById('btnStep1').disabled = false;
    }
  } catch (e) {
    prereqEl.innerHTML = `<div class="check-item"><span class="check-icon">❌</span><div>خطا در بررسی</div></div>`;
  }
}

// ─── Step 2: Source DB ──────────────────────────────
function renderSourceDbs() {
  const panel = state.selectedPanel;
  if (!panel) return goStep(1);

  const grid = document.getElementById('sourceDbGrid');
  const dbs = panel.supported_source_dbs;

  grid.innerHTML = dbs.map(db => {
    const names = { sqlite: 'SQLite', mysql: 'MySQL', mariadb: 'MariaDB', postgresql: 'PostgreSQL', timescaledb: 'TimescaleDB' };
    const auto = state.detected?.marzban_db === db || state.detected?.pasarguard_db === db;
    return `
      <div class="db-card ${auto ? 'recommended' : ''}" data-db="${db}" onclick="selectSourceDb('${db}')">
        <h4>${names[db] || db}</h4>
        ${auto ? '<p>شناسایی‌شده در سرور</p>' : ''}
      </div>`;
  }).join('');

  if (state.detected?.marzban_db) selectSourceDb(state.detected.marzban_db);
  else if (state.detected?.pasarguard_db && panel.id === 'pasarguard') selectSourceDb(state.detected.pasarguard_db);
  else if (dbs.length === 1) selectSourceDb(dbs[0]);
}

function selectSourceDb(db) {
  state.sourceDb = db;
  document.querySelectorAll('#sourceDbGrid .db-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.db === db);
  });

  const needsPwd = ['mysql', 'mariadb', 'postgresql', 'timescaledb'].includes(db);
  document.getElementById('dbCredentials').classList.toggle('hidden', !needsPwd);
}

// ─── Step 3: Target DB ──────────────────────────────
async function renderTargetDbs() {
  const panel = state.selectedPanel;
  if (!panel || !state.sourceDb) return goStep(2);

  state.sourcePassword = document.getElementById('sourcePassword').value;

  const grid = document.getElementById('targetDbGrid');
  grid.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)">در حال بارگذاری پیشنهادات...</div>';

  try {
    const res = await fetch(`/api/recommendations/${panel.id}/${state.sourceDb}`);
    const recs = await res.json();

    const names = { sqlite: 'SQLite', mysql: 'MySQL', mariadb: 'MariaDB', postgresql: 'PostgreSQL', timescaledb: 'TimescaleDB' };
    grid.innerHTML = recs.map(r => `
      <div class="db-card ${r.recommended ? 'recommended' : ''}" data-db="${r.id}" onclick="selectTargetDb('${r.id}')">
        <h4>${r.name_fa}</h4>
        <p>${r.reason_fa}</p>
      </div>`).join('');

    if (recs.length) selectTargetDb(recs[0].id);

    const recEl = document.getElementById('targetRecommendation');
    if (recs[0]?.recommended) {
      recEl.classList.remove('hidden');
      recEl.innerHTML = `💡 <strong>پیشنهاد:</strong> ${recs[0].reason_fa}`;
    }
  } catch (e) {
    grid.innerHTML = '<div class="check-item"><span class="check-icon">❌</span><div>خطا</div></div>';
  }

  const needsPg = panel.requires_pasarguard && !state.detected?.pasarguard;
  document.getElementById('installPgSection').classList.toggle('hidden', !needsPg);
}

function selectTargetDb(db) {
  state.targetDb = db;
  document.querySelectorAll('#targetDbGrid .db-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.db === db);
  });

  const needsPwd = ['mysql', 'mariadb', 'postgresql', 'timescaledb'].includes(db);
  document.getElementById('targetCredentials').classList.toggle('hidden', !needsPwd);
}

async function installPasarguard() {
  const logEl = document.getElementById('installPgLog');
  logEl.classList.remove('hidden');
  logEl.textContent = 'در حال نصب PasarGuard — ممکن است چند دقیقه طول بکشد...\n';

  const db = state.targetDb || 'sqlite';
  try {
    const res = await fetch(`/api/install-pasarguard?database=${db}`, { method: 'POST' });
    const data = await res.json();
    logEl.textContent += data.output || '';
    if (data.ok) {
      logEl.textContent += '\n✅ نصب موفق!';
      state.detected.pasarguard = true;
      document.getElementById('installPgSection').classList.add('hidden');
    } else {
      logEl.textContent += '\n❌ خطا در نصب';
    }
  } catch (e) {
    logEl.textContent += `\nخطا: ${e.message}`;
  }
}

// ─── Step 4: Summary ────────────────────────────────
function renderSummary() {
  state.targetPassword = document.getElementById('targetPassword').value;
  const panel = state.selectedPanel;
  const names = { sqlite: 'SQLite', mysql: 'MySQL', mariadb: 'MariaDB', postgresql: 'PostgreSQL', timescaledb: 'TimescaleDB' };

  document.getElementById('migrationSummary').innerHTML = `
    <div class="summary-row"><span class="summary-label">پنل مبدأ</span><span>${panel?.name_fa || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">دیتابیس مبدأ</span><span>${names[state.sourceDb] || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">دیتابیس مقصد</span><span>${names[state.targetDb] || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">لینک اشتراک</span><span>${panel?.subscription_preserved ? '✅ حفظ می‌شود' : '⚠️ تغییر می‌کند'}</span></div>
    <div class="summary-row"><span class="summary-label">فایل بکاپ</span><span>${state.uploadInfo?.filename || 'استفاده از سرور'}</span></div>
  `;

  document.getElementById('redirectOption').classList.toggle('hidden', panel?.id !== '3x-ui');

  const warnEl = document.getElementById('finalWarnings');
  const warnings = panel?.warnings_fa || [];
  if (warnings.length) {
    warnEl.innerHTML = warnings.map(w => `<p style="font-size:0.85rem;margin:4px 0">⚠️ ${w}</p>`).join('');
    warnEl.classList.remove('hidden');
  } else {
    warnEl.classList.add('hidden');
  }
}

// ─── Step 5: Migration ──────────────────────────────
async function startMigration() {
  goStep(5);
  const terminal = document.getElementById('logTerminal');
  terminal.textContent = '';

  const body = {
    source_panel: state.selectedPanel.id,
    source_db: state.sourceDb,
    target_db: state.targetDb,
    source_db_password: state.sourcePassword || null,
    target_db_password: state.targetPassword || null,
    upload_id: state.uploadId,
    install_redirect: document.getElementById('installRedirect')?.checked || false,
  };

  try {
    const res = await fetch('/api/migrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    state.jobId = data.job_id;

    connectWebSocket(data.job_id);
  } catch (e) {
    showError(e.message);
  }
}

function connectWebSocket(jobId) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/migrate/${jobId}`);
  const terminal = document.getElementById('logTerminal');

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'log') {
      terminal.textContent += msg.message + '\n';
      terminal.scrollTop = terminal.scrollHeight;
    }
    if (msg.type === 'status') {
      document.getElementById('progressFill').style.width = msg.progress + '%';
      document.getElementById('progressText').textContent = msg.progress + '٪';
      document.getElementById('statusMsg').textContent = msg.message_fa || '';
    }
    if (msg.type === 'done') {
      if (msg.status === 'success') showSuccess(msg.result);
      else showError(msg.result?.error || 'خطای ناشناخته', terminal.textContent);
    }
  };

  ws.onerror = () => pollStatus(jobId);
}

async function pollStatus(jobId) {
  const terminal = document.getElementById('logTerminal');
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/migrate/${jobId}`);
      const data = await res.json();
      document.getElementById('progressFill').style.width = data.progress + '%';
      document.getElementById('progressText').textContent = data.progress + '٪';
      document.getElementById('statusMsg').textContent = data.message_fa || '';
      terminal.textContent = data.logs.join('\n');
      terminal.scrollTop = terminal.scrollHeight;
      if (data.status === 'success') { clearInterval(interval); showSuccess(data.result); }
      if (data.status === 'error') { clearInterval(interval); showError(data.result?.error, data.logs.join('\n')); }
    } catch (e) { /* retry */ }
  }, 2000);
}

function showSuccess(result) {
  goStep(6);
  document.getElementById('resultSuccess').classList.remove('hidden');
  document.getElementById('resultError').classList.add('hidden');

  const panelUrl = result?.panel_url || `https://${state.serverIp}:8000/dashboard/`;
  document.getElementById('panelLink').href = panelUrl;
  document.getElementById('resultMessage').textContent =
    result?.subscription_preserved
      ? 'تمام داده‌ها و لینک‌های اشتراک با موفقیت منتقل شدند.'
      : 'داده‌ها منتقل شدند. لطفاً لینک‌های جدید را به کاربران اطلاع دهید.';

  let details = '';
  if (result?.warnings_fa) {
    details = '<ul>' + result.warnings_fa.map(w => `<li>⚠️ ${w}</li>`).join('') + '</ul>';
  }
  if (result?.users_migrated) {
    details += `<p>کاربران منتقل‌شده: ${result.users_migrated} از ${result.users_total}</p>`;
  }
  if (result?.redirect_installed) {
    details += '<p>✅ سرور ریدایرکت لینک‌های قدیمی نصب شد</p>';
  }
  document.getElementById('resultDetails').innerHTML = details;
}

function showError(msg, logs) {
  goStep(6);
  document.getElementById('resultError').classList.remove('hidden');
  document.getElementById('resultSuccess').classList.add('hidden');
  document.getElementById('errorMessage').textContent = msg;
  if (logs) document.getElementById('errorLog').textContent = logs;
}

// ─── Upload ─────────────────────────────────────────
function setupUpload() {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('fileInput');

  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  });
  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => { if (input.files.length) uploadFile(input.files[0]); });
}

async function uploadFile(file) {
  const status = document.getElementById('uploadStatus');
  status.classList.remove('hidden');
  status.textContent = `در حال آپلود ${file.name}...`;
  status.style.background = 'var(--warning-bg)';
  status.style.color = 'var(--warning)';

  const form = new FormData();
  form.append('file', file);

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: form });
    const data = await res.json();
    state.uploadId = data.upload_id;
    state.uploadInfo = data;

    let hint = '';
    if (data.detected?.panel_hint) {
      hint = ` — تشخیص: ${data.detected.panel_hint}`;
      const panel = state.panels.find(p => p.id === data.detected.panel_hint);
      if (panel && !state.selectedPanel) {
        state.selectedPanel = panel;
      }
    }

    status.textContent = `✅ ${file.name} آپلود شد (${(data.size / 1024).toFixed(0)} KB)${hint}`;
    status.style.background = 'var(--success-bg)';
    status.style.color = 'var(--success)';
    document.getElementById('btnStep1').disabled = false;
  } catch (e) {
    status.textContent = `❌ خطا: ${e.message}`;
    status.style.background = 'var(--error-bg)';
    status.style.color = 'var(--error)';
  }
}
