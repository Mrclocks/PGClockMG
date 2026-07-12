/* PG-Migrator Wizard */

const state = {
  lang: localStorage.getItem('pg-migrator-lang') || 'en',
  currentStep: 0,
  panels: [],
  subscriptionLabels: {},
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
  prereqData: null,
  marzbanMode: null,
};

document.addEventListener('DOMContentLoaded', async () => {
  setLang(state.lang);
  await loadInfo();
  document.getElementById('uploadDragText').textContent = t('step2.uploadDrag');
  document.getElementById('uploadSelectText').textContent = t('step2.uploadSelect');
  document.querySelector('#remnawaveFields .form-group:nth-child(1) label').textContent = t('step2.remnawaveUrl');
  document.querySelector('#remnawaveFields .form-group:nth-child(2) label').textContent = t('step2.remnawaveToken');
  document.getElementById('remnawaveUrl').placeholder = t('step2.remnawaveUrlPh');
  document.getElementById('remnawaveToken').placeholder = t('step2.remnawaveTokenPh');
  document.getElementById('footerDocs').textContent = t('footer.docs');
  document.getElementById('footerGithub').textContent = t('footer.github');
  document.getElementById('statusMsg').textContent = t('step5.preparing');
  setupUpload();
});

async function loadInfo() {
  try {
    const res = await fetch('/api/info');
    const data = await res.json();
    state.panels = data.panels;
    state.subscriptionLabels = data.subscription_labels || {};
    state.serverIp = data.server_ip;
    document.getElementById('serverIp').textContent = `${data.server_ip}:${data.web_port}`;
    if (data.version) document.getElementById('appVersion').textContent = `v${data.version}`;
  } catch (e) {
    console.error(e);
  }
}

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

function renderPanels() {
  const grid = document.getElementById('panelGrid');
  const lang = state.lang;
  grid.innerHTML = state.panels.map(p => {
    const sup = t(`support.${p.support_level}`) || p.support_level;
    const supClass = `support-${p.support_level}`;
    const subText = t(`sub.${p.subscription_mode}`) || p.subscription_mode;
    const subClass = p.subscription_mode === 'changed' ? 'sub-no' : 'sub-yes';
    return `
      <div class="panel-card" data-id="${p.id}" onclick="selectPanel('${p.id}')">
        <span class="support-badge ${supClass}">${sup}</span>
        <div class="icon">${p.icon}</div>
        <h3>${tr(p.name, lang)}</h3>
        <p>${tr(p.description, lang)}</p>
        <div class="sub-preserve ${subClass}">${subText}</div>
      </div>`;
  }).join('');
}

async function selectPanel(id) {
  state.selectedPanel = state.panels.find(p => p.id === id);
  state.marzbanMode = null;
  document.querySelectorAll('.panel-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.id === id);
  });

  const panel = state.selectedPanel;
  const lang = state.lang;

  const modeSection = document.getElementById('marzbanModeSection');
  if (panel.id === 'marzban') {
    modeSection.classList.remove('hidden');
    modeSection.querySelector('.mode-title').textContent = t('step1.marzbanModeTitle');
    modeSection.querySelector('.mode-desc').textContent = t('step1.marzbanModeDesc');
    renderMarzbanModes();
  } else {
    modeSection.classList.add('hidden');
  }

  const notesEl = document.getElementById('panelInstallNotes');
  if (panel.prerequisites?.install_notes) {
    notesEl.classList.remove('hidden');
    notesEl.innerHTML = `<strong>📋 ${t('step1.prereqTitle')}</strong><p style="margin-top:8px;font-size:0.9rem">${tr(panel.prerequisites.install_notes, lang)}</p>`;
  } else notesEl.classList.add('hidden');

  const warnEl = document.getElementById('panelWarnings');
  const warnings = tr(panel.warnings, lang);
  if (Array.isArray(warnings) && warnings.length) {
    warnEl.classList.remove('hidden');
    warnEl.innerHTML = warnings.map(w => `<p style="font-size:0.85rem;margin:4px 0">⚠️ ${w}</p>`).join('');
  } else warnEl.classList.add('hidden');

  await renderPanelPrereqs(id);
}

async function renderPanelPrereqs(id) {
  const prereqEl = document.getElementById('panelPrereqs');
  prereqEl.classList.remove('hidden');
  prereqEl.innerHTML = '<div style="text-align:center;padding:12px;color:var(--text-dim)">...</div>';

  try {
    const res = await fetch(`/api/prerequisites/${id}`);
    const data = await res.json();
    state.prereqData = data;
    state.detected = data.detected || {};
    const lang = state.lang;

    prereqEl.innerHTML = data.checks.map(c => `
      <div class="check-item">
        <span class="check-icon">${c.ok ? '✅' : (c.optional ? '⚠️' : '❌')}</span>
        <div>
          <div>${tr(c.label, lang)}</div>
          <div class="check-detail">${tr(c.detail, lang)}</div>
        </div>
      </div>`).join('');

    if (!data.ok) {
      prereqEl.innerHTML += `<div class="info-box" style="margin-top:12px">💡 ${t('step1.uploadHint')}</div>`;
    }

    if (id === 'marzban') {
      renderMarzbanModes();
    }

    document.getElementById('btnStep1').disabled = id === 'marzban' && !state.marzbanMode;
  } catch (e) {
    prereqEl.innerHTML = `<div class="check-item"><span class="check-icon">❌</span><div>Error</div></div>`;
  }
}

function suggestMarzbanMode() {
  const d = state.detected || {};
  if (d.marzban && !d.pasarguard) return 'inplace';
  if (d.pasarguard || state.uploadId) return 'fresh';
  if (d.marzban && d.pasarguard) return 'fresh';
  return 'fresh';
}

function renderMarzbanModes() {
  const grid = document.getElementById('marzbanModeGrid');
  if (!grid) return;
  const suggested = state.prereqData?.detected?.suggested_marzban_mode || suggestMarzbanMode();
  const modes = [
    { id: 'inplace', icon: '🔄', title: t('step1.marzbanInplace'), desc: t('step1.marzbanInplaceDesc') },
    { id: 'fresh', icon: '🆕', title: t('step1.marzbanFresh'), desc: t('step1.marzbanFreshDesc') },
  ];
  grid.innerHTML = modes.map(m => `
    <div class="mode-card ${state.marzbanMode === m.id ? 'selected' : ''}" data-mode="${m.id}" onclick="selectMarzbanMode('${m.id}')">
      <div class="mode-icon">${m.icon}</div>
      <h4>${m.title}</h4>
      <p>${m.desc}</p>
      <span class="mode-badge ${m.id === suggested ? 'mode-badge-suggested' : 'mode-badge-alt'}">
        ${m.id === suggested ? t('step1.suggested') : t('step1.alternative')}
      </span>
    </div>`).join('');
  if (!state.marzbanMode) selectMarzbanMode(suggested);
}

function selectMarzbanMode(mode) {
  state.marzbanMode = mode;
  document.querySelectorAll('.mode-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.mode === mode);
  });
  document.getElementById('btnStep1').disabled = false;
}

function renderSourceDbs() {
  const panel = state.selectedPanel;
  if (!panel) return goStep(1);
  const lang = state.lang;

  document.getElementById('remnawaveFields').classList.toggle('hidden', panel.id !== 'remnawave');

  const grid = document.getElementById('sourceDbGrid');
  grid.innerHTML = panel.supported_source_dbs.map(db => {
    const names = { sqlite: 'SQLite', mysql: 'MySQL', mariadb: 'MariaDB', postgresql: 'PostgreSQL', timescaledb: 'TimescaleDB' };
    const auto = state.detected?.marzban_db === db || state.detected?.pasarguard_db === db;
    return `
      <div class="db-card ${auto ? 'recommended' : ''}" data-db="${db}" onclick="selectSourceDb('${db}')">
        <h4>${names[db] || db}</h4>
        ${auto ? `<p>${lang === 'fa' ? 'شناسایی‌شده' : lang === 'ru' ? 'Обнаружено' : 'Detected'}</p>` : ''}
      </div>`;
  }).join('');

  if (state.detected?.marzban_db) selectSourceDb(state.detected.marzban_db);
  else if (state.detected?.pasarguard_db && panel.id === 'pasarguard') selectSourceDb(state.detected.pasarguard_db);
  else if (panel.supported_source_dbs.length === 1) selectSourceDb(panel.supported_source_dbs[0]);
}

function selectSourceDb(db) {
  state.sourceDb = db;
  document.querySelectorAll('#sourceDbGrid .db-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.db === db);
  });
  const needsPwd = ['mysql', 'mariadb', 'postgresql', 'timescaledb'].includes(db);
  document.getElementById('dbCredentials').classList.toggle('hidden', !needsPwd || state.selectedPanel?.id === 'remnawave');
}

async function renderTargetDbs() {
  const panel = state.selectedPanel;
  if (!panel || !state.sourceDb) return goStep(2);
  state.sourcePassword = document.getElementById('sourcePassword').value;

  const grid = document.getElementById('targetDbGrid');
  grid.innerHTML = '...';

  try {
    const res = await fetch(`/api/recommendations/${panel.id}/${state.sourceDb}`);
    const recs = await res.json();
    const lang = state.lang;

    grid.innerHTML = recs.map(r => `
      <div class="db-card ${r.recommended ? 'recommended' : ''}" data-db="${r.id}" onclick="selectTargetDb('${r.id}')">
        <h4>${tr(r.name, lang)}</h4>
        <p>${tr(r.reason, lang)}</p>
      </div>`).join('');

    if (recs.length) selectTargetDb(recs[0].id);

    const recEl = document.getElementById('targetRecommendation');
    if (recs[0]?.recommended) {
      recEl.classList.remove('hidden');
      recEl.innerHTML = `💡 ${tr(recs[0].reason, lang)}`;
    } else recEl.classList.add('hidden');
  } catch (e) {
    grid.innerHTML = 'Error';
  }

  const needsPg = panel.prerequisites?.pasarguard_required && !state.detected?.pasarguard;
  const marzbanFresh = panel.id === 'marzban' && state.marzbanMode === 'fresh';
  const marzbanInplace = panel.id === 'marzban' && state.marzbanMode === 'inplace';
  document.getElementById('installPgSection').classList.toggle('hidden', !needsPg && !marzbanFresh);
  if (marzbanInplace) {
    document.getElementById('installPgSection').classList.add('hidden');
  }
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
  logEl.textContent = t('step3.installing') + '\n';
  const db = state.targetDb || 'sqlite';
  try {
    const res = await fetch(`/api/install-pasarguard?database=${db}`, { method: 'POST' });
    const data = await res.json();
    logEl.textContent += data.output || '';
    if (data.ok) {
      state.detected.pasarguard = true;
      document.getElementById('installPgSection').classList.add('hidden');
    }
  } catch (e) {
    logEl.textContent += e.message;
  }
}

function renderSummary() {
  state.targetPassword = document.getElementById('targetPassword').value;
  const panel = state.selectedPanel;
  const lang = state.lang;
  const s = t('step4.summary');
  const names = { sqlite: 'SQLite', mysql: 'MySQL', mariadb: 'MariaDB', postgresql: 'PostgreSQL', timescaledb: 'TimescaleDB' };

  const linkLabel = t(`sub.${panel.subscription_mode}`);
  const methodNames = {
    inplace: t('step1.marzbanInplace'),
    fresh: t('step1.marzbanFresh'),
  };
  const methodRow = panel.id === 'marzban' && state.marzbanMode
    ? `<div class="summary-row"><span class="summary-label">${s.method}</span><span>${methodNames[state.marzbanMode] || state.marzbanMode}</span></div>`
    : '';

  document.getElementById('migrationSummary').innerHTML = `
    <div class="summary-row"><span class="summary-label">${s.source}</span><span>${tr(panel.name, lang)}</span></div>
    ${methodRow}
    <div class="summary-row"><span class="summary-label">${s.sourceDb}</span><span>${names[state.sourceDb] || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">${s.targetDb}</span><span>${names[state.targetDb] || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">${s.links}</span><span>${linkLabel}</span></div>
    <div class="summary-row"><span class="summary-label">${s.backup}</span><span>${state.uploadInfo?.filename || s.server}</span></div>`;

  document.getElementById('redirectOption').classList.toggle('hidden', panel.id !== '3x-ui');

  const warnEl = document.getElementById('finalWarnings');
  const warnings = tr(panel.warnings, lang);
  if (Array.isArray(warnings) && warnings.length) {
    warnEl.innerHTML = warnings.map(w => `<p style="font-size:0.85rem;margin:4px 0">⚠️ ${w}</p>`).join('');
    warnEl.classList.remove('hidden');
  } else warnEl.classList.add('hidden');
}

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
    install_redirect: document.getElementById('installRedirect')?.checked ?? true,
    remnawave_url: document.getElementById('remnawaveUrl')?.value || null,
    remnawave_token: document.getElementById('remnawaveToken')?.value || null,
    marzban_mode: state.marzbanMode || 'auto',
  };

  try {
    const res = await fetch('/api/migrate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
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
      document.getElementById('progressText').textContent = msg.progress + '%';
      if (msg.message) document.getElementById('statusMsg').textContent = msg.message;
    }
    if (msg.type === 'done') {
      if (msg.status === 'success') showSuccess(msg.result);
      else showError(msg.result?.error || 'Error', terminal.textContent);
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
      document.getElementById('progressText').textContent = data.progress + '%';
      if (data.message) document.getElementById('statusMsg').textContent = data.message;
      terminal.textContent = data.logs.join('\n');
      if (data.status === 'success') { clearInterval(interval); showSuccess(data.result); }
      if (data.status === 'error') { clearInterval(interval); showError(data.result?.error, data.logs.join('\n')); }
    } catch (e) { /* retry */ }
  }, 2000);
}

function showSuccess(result) {
  goStep(6);
  document.getElementById('resultSuccess').classList.remove('hidden');
  document.getElementById('resultError').classList.add('hidden');
  document.querySelector('#resultSuccess h2').textContent = t('step6.success');

  const panelUrl = result?.panel_url || `https://${state.serverIp}:8000/dashboard/`;
  document.getElementById('panelLink').href = panelUrl;

  const mode = result?.subscription_mode || state.selectedPanel?.subscription_mode;
  const msgKey = mode === 'native' ? 'successLinks' : mode === 'redirect' ? 'successRedirect' : 'successChanged';
  document.getElementById('resultMessage').textContent = t(`step6.${msgKey}`);

  let details = '';
  const warnings = result?.warnings;
  if (warnings) {
    const w = tr(warnings, state.lang);
    if (Array.isArray(w)) details = '<ul>' + w.map(x => `<li>⚠️ ${x}</li>`).join('') + '</ul>';
  }
  if (result?.redirect_installed) {
    details += `<p>✅ Redirect server installed</p>`;
  }
  if (result?.users_migrated) {
    details += `<p>${result.users_migrated} / ${result.users_total} users</p>`;
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

function setupUpload() {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('fileInput');
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => { e.preventDefault(); zone.classList.remove('dragover'); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
  zone.addEventListener('click', e => { if (e.target.id !== 'uploadSelectText') input.click(); });
  input.addEventListener('change', () => { if (input.files.length) uploadFile(input.files[0]); });
}

async function uploadFile(file) {
  const status = document.getElementById('uploadStatus');
  status.classList.remove('hidden');
  status.textContent = `${t('uploading')} ${file.name}...`;

  const form = new FormData();
  form.append('file', file);

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: form });
    const data = await res.json();
    state.uploadId = data.upload_id;
    state.uploadInfo = data;
    let hint = data.detected?.panel_hint ? ` — ${t('detected')}: ${data.detected.panel_hint}` : '';
    status.textContent = `✅ ${file.name} ${t('uploaded')} (${(data.size / 1024).toFixed(0)} KB)${hint}`;
    status.style.background = 'var(--success-bg)';
    status.style.color = 'var(--success)';
    document.getElementById('btnStep1').disabled = false;
    if (state.selectedPanel?.id === 'marzban') renderMarzbanModes();
  } catch (e) {
    status.textContent = `❌ ${t('uploadErr')}: ${e.message}`;
    status.style.background = 'var(--error-bg)';
    status.style.color = 'var(--error)';
  }
}
