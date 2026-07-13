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
  uploadBundleId: null,
  uploadMode: 'zip',
  uploadRequirements: null,
  bundleStatus: null,
  jobId: null,
  serverIp: '',
  detected: {},
  prereqData: null,
  pasarguardInstallDbs: [],
  sourceEnvSummary: null,
  pasarguardEnvSummary: null,
  systemCheck: null,
};

function panelLatinName(panel) {
  if (panel?.id === '3x-ui') return 'X-UI';
  return panel?.name?.en || panel?.id || '';
}

function getPgInstallCmd() {
  return 'sudo bash -c "$(curl -fsSL https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh)" @ install';
}

function dbNeedsPassword(db) {
  return ['mysql', 'mariadb', 'postgresql', 'timescaledb'].includes(db);
}

function defaultDbPort(db) {
  if (db === 'mysql' || db === 'mariadb') return '3306';
  if (db === 'postgresql' || db === 'timescaledb') return '6432';
  return '';
}

function togglePassword(inputId, btn) {
  const el = document.getElementById(inputId);
  if (!el) return;
  const show = el.type === 'password';
  el.type = show ? 'text' : 'password';
  btn.textContent = show ? '🙈' : '👁';
}

function readDbCredentials(role) {
  const p = role === 'source' ? 'source' : 'target';
  if (p === 'target') {
    const env = state.systemCheck?.pasarguard_env || state.pasarguardEnvSummary;
    const portRaw = env?.db_port || document.getElementById('targetDbPort')?.value?.trim();
    const port = portRaw ? parseInt(portRaw, 10) : null;
    return {
      target_db_user: 'pasarguard',
      target_db_name: 'pasarguard',
      target_db_host: env?.db_host || '127.0.0.1',
      target_db_port: Number.isFinite(port) ? port : null,
      target_db_password: document.getElementById('targetDbPassword')?.value || null,
    };
  }
  const portRaw = document.getElementById(`${p}DbPort`)?.value?.trim();
  const port = portRaw ? parseInt(portRaw, 10) : null;
  return {
    [`${p}_db_user`]: document.getElementById(`${p}DbUser`)?.value?.trim() || null,
    [`${p}_db_name`]: document.getElementById(`${p}DbName`)?.value?.trim() || null,
    [`${p}_db_host`]: document.getElementById(`${p}DbHost`)?.value?.trim() || '127.0.0.1',
    [`${p}_db_port`]: Number.isFinite(port) ? port : null,
    [`${p}_db_password`]: document.getElementById(`${p}DbPassword`)?.value || null,
  };
}

function hasDbCredentials(role) {
  const db = role === 'source' ? state.sourceDb : state.targetDb;
  if (!dbNeedsPassword(db)) return true;
  const c = readDbCredentials(role);
  const prefix = role === 'source' ? 'source' : 'target';
  if (role === 'target') {
    return !!c.target_db_password;
  }
  return !!(c[`${prefix}_db_user`] && c[`${prefix}_db_name`] && c[`${prefix}_db_password`]);
}

function updateSourceCredentialsVisibility() {
  const box = document.getElementById('sourceDbCredentials');
  if (!box) return;
  const needs = dbNeedsPassword(state.sourceDb) && state.selectedPanel?.id !== 'remnawave';
  box.classList.toggle('hidden', !needs);
  if (needs) {
    const portEl = document.getElementById('sourceDbPort');
    if (portEl && !portEl.value) portEl.placeholder = defaultDbPort(state.sourceDb);
  }
}

function updateTargetCredentialsVisibility() {
  const box = document.getElementById('targetDbCredentials');
  if (!box) return;
  const db = getDetectedTargetDb();
  const needs = db && dbNeedsPassword(db) && !needsPasarguardInstall();
  box.classList.toggle('hidden', !needs);
}

function setupCredentialListeners() {
  [
    'sourceDbUser', 'sourceDbName', 'sourceDbHost', 'sourceDbPort', 'sourceDbPassword',
    'targetDbPassword',
  ].forEach((id) => {
    const el = document.getElementById(id);
    if (!el || el.dataset.bound) return;
    el.dataset.bound = '1';
    el.addEventListener('input', () => updateStepButtons());
  });
}

function needsPasarguardInstall() {
  const panel = state.selectedPanel;
  if (!panel) return false;
  if (panel.id === 'marzban') return false;
  if (panel.id === 'pasarguard') return !state.detected?.pasarguard;
  if (panel.prerequisites?.pasarguard_required) return !state.detected?.pasarguard;
  return false;
}

function dbDisplayName(db) {
  const names = { sqlite: 'SQLite', mysql: 'MySQL', mariadb: 'MariaDB', postgresql: 'PostgreSQL', timescaledb: 'TimescaleDB' };
  return names[db] || db;
}

function detectMarzbanSourceDb() {
  const analysis = state.bundleStatus?.analysis || state.uploadInfo?.analysis;
  if (analysis?.detected_source_db) return analysis.detected_source_db;
  if (state.prereqData?.detected?.upload_source_db) return state.prereqData.detected.upload_source_db;
  if (state.detected?.marzban_db) return state.detected.marzban_db;
  if (state.detected?.marzban && (state.systemCheck?.marzban_db || state.detected.marzban_db)) {
    return state.systemCheck?.marzban_db || state.detected.marzban_db;
  }
  return null;
}

document.addEventListener('DOMContentLoaded', async () => {
  await loadInfo();
  if (!state.systemCheck) await loadSystemCheck();
  setLang(state.lang);
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
  setupCredentialListeners();
  updateStepButtons();
});

function applySystemCheck(sys) {
  if (!sys) return;
  state.systemCheck = sys;
  state.pasarguardEnvSummary = null;
  state.detected = {
    ...state.detected,
    pasarguard: sys.pasarguard,
    marzban: sys.marzban,
    pasarguard_db: sys.pasarguard_db,
    marzban_db: sys.marzban_db,
  };
}

async function loadInfo() {
  try {
    const res = await fetch('/api/info');
    const data = await res.json();
    state.panels = data.panels;
    state.subscriptionLabels = data.subscription_labels || {};
    state.serverIp = data.server_ip;
    document.getElementById('serverIp').textContent = `${data.server_ip}:${data.web_port}`;
    if (data.version) document.getElementById('appVersion').textContent = `v${data.version}`;
    if (data.pasarguard_install_dbs) state.pasarguardInstallDbs = data.pasarguard_install_dbs;
    if (data.system) applySystemCheck(data.system);
  } catch (e) {
    console.error(e);
  }
}

async function loadSystemCheck() {
  try {
    const res = await fetch('/api/system-check');
    const data = await res.json();
    applySystemCheck(data);
    renderGlobalChecks();
    if (state.currentStep === 3) renderDetectedTargetDb();
    updateStepButtons();
  } catch (e) {
    console.error(e);
    const el = document.getElementById('globalChecks');
    if (el) {
      el.innerHTML += `<div class="check-item"><span class="check-icon">❌</span><div><div>Server check</div><div class="check-detail">${e.message}</div></div></div>`;
    }
  }
}

function showStepBlock(step, msg) {
  const el = document.getElementById(`step${step}Block`);
  if (!el) return;
  if (msg) {
    el.textContent = msg;
    el.classList.remove('hidden');
  } else {
    el.textContent = '';
    el.classList.add('hidden');
  }
}

function canProceedStep0() {
  const s = state.systemCheck;
  if (!s) return t('step0.checking');
  if (!s.root) return t('block.noRoot');
  if (!s.docker) return t('block.noDocker');
  return null;
}

function canProceedStep1() {
  if (!state.selectedPanel) return t('block.noPanel');
  if (!state.prereqData?.ok) return t('block.prereqFailed');
  return null;
}

function canProceedStep2() {
  const panel = state.selectedPanel;
  if (panel?.id === 'marzban') {
    const db = detectMarzbanSourceDb();
    if (!db) return t('block.detectSourceDb');
    state.sourceDb = db;
  } else if (!state.sourceDb) {
    return t('block.noSourceDb');
  }
  if (panel?.id === 'remnawave') {
    const url = document.getElementById('remnawaveUrl')?.value?.trim();
    const token = document.getElementById('remnawaveToken')?.value?.trim();
    if (!url || !token) return t('block.remnawaveCreds');
  }
  const needsPwd = dbNeedsPassword(state.sourceDb);
  const analysis = state.bundleStatus?.analysis || state.uploadInfo?.analysis;
  if (needsPwd && panel?.id !== 'remnawave' && !hasDbCredentials('source')) {
    return t('block.sourceCredsIncomplete');
  }
  if (!uploadSatisfied()) {
    return t('block.uploadsIncomplete');
  }
  if (analysis?.detected_source_db && state.sourceDb !== analysis.detected_source_db) {
    return t('block.dbMismatch');
  }
  return null;
}

function uploadSatisfied() {
  const reqs = state.uploadRequirements;
  if (!reqs || reqs.upload_mode === 'none') return true;
  if (reqs.upload_mode === 'optional' && !state.uploadBundleId) return true;
  return !!state.bundleStatus?.complete;
}

function bundleHasEnvPassword() {
  const slots = state.bundleStatus?.slots || [];
  return slots.some(s => s.id === 'env' && s.ok);
}

function canProceedStep3() {
  const db = getDetectedTargetDb();
  if (!db) return t('block.noTargetDbDetected');
  state.targetDb = db;
  if (needsPasarguardInstall()) return t('block.pasarguardMissing');
  if (dbNeedsPassword(db) && !hasDbCredentials('target')) {
    return t('block.targetCredsIncomplete');
  }
  return null;
}

function getDetectedTargetDb() {
  return state.systemCheck?.pasarguard_db
    || state.detected?.pasarguard_db
    || null;
}

function applyTargetEnvDefaults() {
  const env = state.systemCheck?.pasarguard_env;
  if (!env) return;
  const hostEl = document.getElementById('targetDbHost');
  const portEl = document.getElementById('targetDbPort');
  if (hostEl && env.db_host) hostEl.value = env.db_host;
  if (portEl && env.db_port) portEl.value = env.db_port;
}

function renderDetectedTargetDb() {
  const card = document.getElementById('detectedTargetDb');
  if (!card) return;

  const db = getDetectedTargetDb();
  const env = state.systemCheck?.pasarguard_env;
  const s = t('step3.detected');

  if (!db) {
    card.classList.add('hidden');
    card.innerHTML = '';
    return;
  }

  state.targetDb = db;
  applyTargetEnvDefaults();

  const host = env?.db_host || '127.0.0.1';
  const port = env?.db_port || defaultDbPort(db) || '—';

  card.innerHTML = `
    <h3>${s.title}</h3>
    <div class="detected-db-type">${dbDisplayName(db)}</div>
    <div class="detected-db-row"><span class="label">${s.user}</span><span class="value">pasarguard</span></div>
    <div class="detected-db-row"><span class="label">${s.dbName}</span><span class="value">pasarguard</span></div>
    <div class="detected-db-row"><span class="label">${s.host}</span><span class="value">${host}</span></div>
    <div class="detected-db-row"><span class="label">${s.port}</span><span class="value">${port}</span></div>
  `;
  card.classList.remove('hidden');
}

function copyTargetEnvCmd() {
  const cmd = document.getElementById('targetEnvCmd')?.textContent?.trim()
    || 'sudo nano /opt/pasarguard/.env';
  const btn = document.getElementById('btnCopyTargetEnv');
  navigator.clipboard.writeText(cmd).then(() => {
    if (!btn) return;
    const orig = btn.textContent;
    btn.textContent = t('copied');
    btn.classList.add('copied');
    setTimeout(() => {
      btn.textContent = orig;
      btn.classList.remove('copied');
    }, 1600);
  }).catch(() => {});
}

function updateStepButtons() {
  const b0 = document.getElementById('btnStep0');
  const b1 = document.getElementById('btnStep1');
  const b2 = document.getElementById('btnStep2');
  const b3 = document.getElementById('btnStep3');
  const b4 = document.getElementById('btnStep4');
  if (b0) b0.disabled = !!canProceedStep0();
  if (b1) b1.disabled = !!canProceedStep1();
  if (b2) b2.disabled = !!canProceedStep2();
  if (b3) b3.disabled = !!canProceedStep3();
  if (state.currentStep === 0) showStepBlock(0, canProceedStep0());
  if (state.currentStep === 1) showStepBlock(1, canProceedStep1());
  if (state.currentStep === 2) showStepBlock(2, canProceedStep2());
  if (state.currentStep === 3) showStepBlock(3, canProceedStep3());
}

async function goStep(n) {
  if (n > state.currentStep) {
    let block = null;
    if (n >= 1) block = canProceedStep0();
    if (!block && n >= 2) block = canProceedStep1();
    if (!block && n >= 3) {
      block = canProceedStep2();
    }
    if (!block && n >= 4) {
      block = canProceedStep2() || canProceedStep3();
    }
    if (!block && n >= 5) {
      const v = await validateMigrationRequest();
      if (!v.ok) block = tr(v.errors[0], state.lang) || t('block.validationFailed');
    }
    if (block) {
      showStepBlock(state.currentStep, block);
      updateStepButtons();
      return;
    }
  }

  if (n === 0) loadSystemCheck();
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
  showStepBlock(n, null);
  updateStepButtons();
}

async function validateMigrationRequest() {
  const body = buildMigrationBody();
  try {
    const res = await fetch('/api/validate-migration', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return await res.json();
  } catch (e) {
    return { ok: false, errors: [{ en: e.message, fa: e.message, ru: e.message }] };
  }
}

function buildMigrationBody() {
  const src = readDbCredentials('source');
  const tgt = readDbCredentials('target');
  return {
    source_panel: state.selectedPanel?.id,
    source_db: state.sourceDb,
    target_db: state.targetDb,
    ...src,
    ...tgt,
    upload_id: state.uploadId,
    upload_bundle_id: state.uploadBundleId,
    install_redirect: document.getElementById('installRedirect')?.checked ?? true,
    remnawave_url: document.getElementById('remnawaveUrl')?.value || null,
    remnawave_token: document.getElementById('remnawaveToken')?.value || null,
    marzban_mode: 'fresh',
  };
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
        <div class="panel-card-top">
          <h3>${panelLatinName(p)}</h3>
          <span class="support-badge ${supClass}">${sup}</span>
        </div>
        <p class="panel-caption">${tr(p.description, lang)}</p>
        <div class="panel-card-footer sub-preserve ${subClass}">${subText}</div>
      </div>`;
  }).join('');
}

async function selectPanel(id) {
  state.selectedPanel = state.panels.find(p => p.id === id);
  document.querySelectorAll('.panel-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.id === id);
  });

  const panel = state.selectedPanel;
  const lang = state.lang;

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
    const params = new URLSearchParams();
    if (state.uploadId) params.set('upload_id', state.uploadId);
    if (state.uploadBundleId) params.set('upload_bundle_id', state.uploadBundleId);
    const qs = params.toString() ? `?${params}` : '';
    const res = await fetch(`/api/prerequisites/${id}${qs}`);
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

    document.getElementById('btnStep1').disabled = !!canProceedStep1();
    updateStepButtons();
  } catch (e) {
    prereqEl.innerHTML = `<div class="check-item"><span class="check-icon">❌</span><div>Error</div></div>`;
  }
}

function renderMarzbanDetectedSource() {
  const section = document.getElementById('marzbanSourceSection');
  const grid = document.getElementById('sourceDbGrid');
  if (!section || !grid) return;

  section.classList.remove('hidden');
  grid.classList.add('hidden');

  const labelEl = document.getElementById('marzbanDetectedDbLabel');
  if (labelEl) labelEl.textContent = t('step2.marzbanDetectedLabel');

  const db = detectMarzbanSourceDb();
  const valueEl = document.getElementById('marzbanDetectedDbValue');
  const hintEl = document.getElementById('marzbanDetectedDbHint');
  if (valueEl) {
    valueEl.textContent = db ? dbDisplayName(db) : '—';
    valueEl.classList.toggle('pending', !db);
  }
  if (hintEl) {
    hintEl.textContent = db ? t('step2.marzbanDetectedOk') : t('step2.marzbanDetectedWait');
  }
  if (db) state.sourceDb = db;

  const needsPwd = dbNeedsPassword(db);
  updateSourceCredentialsVisibility();
}

function renderSourceDbs() {
  const panel = state.selectedPanel;
  if (!panel) return goStep(1);

  document.getElementById('remnawaveFields').classList.toggle('hidden', panel.id !== 'remnawave');

  if (panel.id === 'marzban') {
    const h2 = document.querySelector('#step2 h2');
    const desc = document.querySelector('#step2 .desc');
    if (h2) h2.textContent = t('step2.marzbanH2');
    if (desc) desc.textContent = t('step2.marzbanDesc');
    renderMarzbanDetectedSource();
    renderUploadSection();
    return;
  }

  document.getElementById('marzbanSourceSection')?.classList.add('hidden');
  const grid = document.getElementById('sourceDbGrid');
  grid.classList.remove('hidden');

  const lang = state.lang;
  grid.innerHTML = panel.supported_source_dbs.map(db => {
    const auto = state.detected?.marzban_db === db || state.detected?.pasarguard_db === db;
    return `
      <div class="db-card ${auto ? 'recommended' : ''}" data-db="${db}" onclick="selectSourceDb('${db}')">
        <h4>${dbDisplayName(db)}</h4>
        ${auto ? `<p>${lang === 'fa' ? 'شناسایی‌شده' : lang === 'ru' ? 'Обнаружено' : 'Detected'}</p>` : ''}
      </div>`;
  }).join('');

  if (state.detected?.marzban_db) selectSourceDb(state.detected.marzban_db);
  else if (state.detected?.pasarguard_db && panel.id === 'pasarguard') selectSourceDb(state.detected.pasarguard_db);
  else if (panel.supported_source_dbs.length === 1) selectSourceDb(panel.supported_source_dbs[0]);
  else renderUploadSection();
}

function selectSourceDb(db) {
  state.sourceDb = db;
  document.querySelectorAll('#sourceDbGrid .db-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.db === db);
  });
  updateSourceCredentialsVisibility();
  renderUploadSection();
  updateStepButtons();
}

async function renderTargetDbs() {
  const panel = state.selectedPanel;
  if (!panel || !state.sourceDb) return goStep(2);

  const h2 = document.querySelector('#step3 h2');
  const desc = document.querySelector('#step3 .desc');
  const crossEl = document.getElementById('crossDbWarning');

  if (panel.id === 'marzban') {
    if (h2) h2.textContent = t('step3.marzbanH2');
    if (desc) desc.textContent = t('step3.marzbanDescAuto');
  } else {
    if (h2) h2.textContent = t('step3.h2');
    if (desc) desc.textContent = t('step3.descAuto');
  }

  renderDetectedTargetDb();
  updateCrossDbWarning();

  const needsPg = needsPasarguardInstall();
  const installSection = document.getElementById('installPgSection');
  installSection?.classList.toggle('hidden', !needsPg);
  if (needsPg) {
    const cmdEl = document.getElementById('installPgCmd');
    if (cmdEl) cmdEl.textContent = getPgInstallCmd();
  }

  updateTargetCredentialsVisibility();
  updateStepButtons();
}

function updateCrossDbWarning() {
  const crossEl = document.getElementById('crossDbWarning');
  if (!crossEl || state.selectedPanel?.id !== 'marzban') return;
  if (state.sourceDb && state.targetDb && state.sourceDb !== state.targetDb) {
    crossEl.classList.remove('hidden');
    const msg = t('step3.crossDbWarning')
      .replace('{source}', dbDisplayName(state.sourceDb))
      .replace('{target}', dbDisplayName(state.targetDb));
    crossEl.innerHTML = `⚠️ ${msg}`;
  } else {
    crossEl.classList.add('hidden');
  }
}

function selectTargetDb(db) {
  state.targetDb = db;
  updateCrossDbWarning();
  updateTargetCredentialsVisibility();
  updateStepButtons();
}

async function recheckPasarguard() {
  const btn = document.getElementById('btnRecheckPg');
  if (btn) btn.disabled = true;
  await loadSystemCheck();
  if (state.selectedPanel) {
    await renderPanelPrereqs(state.selectedPanel.id);
  }
  if (state.currentStep === 3) {
    await renderTargetDbs();
  } else {
    updateStepButtons();
  }
  if (btn) btn.disabled = false;
}

function renderSummary() {
  const panel = state.selectedPanel;
  const lang = state.lang;
  const s = t('step4.summary');
  const names = { sqlite: 'SQLite', mysql: 'MySQL', mariadb: 'MariaDB', postgresql: 'PostgreSQL', timescaledb: 'TimescaleDB' };

  const linkLabel = t(`sub.${panel.subscription_mode}`);

  document.getElementById('migrationSummary').innerHTML = `
    <div class="summary-row"><span class="summary-label">${s.source}</span><span>${panelLatinName(panel)}</span></div>
    <div class="summary-row"><span class="summary-label">${s.sourceDb}</span><span>${names[state.sourceDb] || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">${panel.id === 'marzban' ? s.pgInstallDb : s.targetDb}</span><span>${names[state.targetDb] || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">${s.links}</span><span>${linkLabel}</span></div>
    <div class="summary-row"><span class="summary-label">${s.backup}</span><span>${state.uploadInfo?.filename || state.bundleStatus?.mode === 'zip' ? t('upload.fullZip') : state.bundleStatus?.mode === 'separate' ? t('upload.separateFiles') : s.server}</span></div>`;

  document.getElementById('redirectOption').classList.toggle('hidden', panel.id !== '3x-ui');

  const warnEl = document.getElementById('finalWarnings');
  const warnings = tr(panel.warnings, lang);
  if (Array.isArray(warnings) && warnings.length) {
    warnEl.innerHTML = warnings.map(w => `<p style="font-size:0.85rem;margin:4px 0">⚠️ ${w}</p>`).join('');
    warnEl.classList.remove('hidden');
  } else warnEl.classList.add('hidden');
}

async function startMigration() {
  const v = await validateMigrationRequest();
  if (!v.ok) {
    showStepBlock(4, tr(v.errors[0], state.lang) || t('block.validationFailed'));
    return;
  }

  await goStep(5);
  const terminal = document.getElementById('logTerminal');
  terminal.textContent = '';

  const body = buildMigrationBody();

  try {
    const res = await fetch('/api/migrate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) {
      const err = await res.json();
      const msg = err.detail?.errors?.[0] ? tr(err.detail.errors[0], state.lang) : (err.detail || res.statusText);
      showError(msg);
      return;
    }
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
      if (msg.status === 'success' && !msg.result?.error) showSuccess(msg.result);
      else showError(msg.result?.error || msg.message || 'Error', terminal.textContent);
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
      if (data.status === 'success' && !data.result?.error) { clearInterval(interval); showSuccess(data.result); }
      if (data.status === 'error' || data.result?.error) {
        clearInterval(interval);
        showError(data.result?.error || data.message, data.logs.join('\n'));
      }
    } catch (e) { /* retry */ }
  }, 2000);
}

function showSuccess(result) {
  goStep(6);
  document.getElementById('resultSuccess').classList.remove('hidden');
  document.getElementById('resultError').classList.add('hidden');
  document.querySelector('#resultSuccess h2').textContent = t('step6.success');

  const panelUrl = result?.panel_url
    || `https://${state.serverIp.split(':')[0]}:${result?.panel_port || state.pasarguardEnvSummary?.panel_port || state.systemCheck?.pasarguard_env?.panel_port || '8000'}/dashboard/`;
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
  document.getElementById('uploadModeZip')?.addEventListener('click', () => setUploadMode('zip'));
  document.getElementById('uploadModeSeparate')?.addEventListener('click', () => setUploadMode('separate'));

  bindZipUploadZone();
}

function bindZipUploadZone() {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('fileInputZip');
  const link = document.getElementById('uploadSelectText');
  if (!zone || !input) return;

  zone.onclick = () => input.click();
  if (link) {
    link.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      input.click();
    };
  }
  input.onchange = () => {
    if (input.files.length) uploadSlotFile('bundle_zip', input.files[0]);
    input.value = '';
  };

  zone.ondragover = (e) => { e.preventDefault(); zone.classList.add('dragover'); };
  zone.ondragleave = () => zone.classList.remove('dragover');
  zone.ondrop = (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer.files.length) uploadSlotFile('bundle_zip', e.dataTransfer.files[0]);
  };
}

function setUploadMode(mode) {
  state.uploadMode = mode;
  document.querySelectorAll('.upload-mode-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  document.getElementById('uploadZipPanel')?.classList.toggle('hidden', mode !== 'zip');
  document.getElementById('uploadSeparatePanel')?.classList.toggle('hidden', mode !== 'separate');
}

async function renderUploadSection() {
  const panel = state.selectedPanel;
  const section = document.getElementById('uploadSection');
  const notNeeded = document.getElementById('uploadNotNeeded');
  if (!panel || !section) return;

  const params = new URLSearchParams({
    panel_id: panel.id,
    source_db: state.sourceDb || '',
    marzban_mode: 'fresh',
  });
  try {
    const res = await fetch(`/api/upload-requirements?${params}`);
    state.uploadRequirements = await res.json();
  } catch (e) {
    state.uploadRequirements = { upload_mode: 'none', slots: [] };
  }

  const reqs = state.uploadRequirements;
  if (reqs.upload_mode === 'none') {
    section.classList.add('hidden');
    notNeeded?.classList.remove('hidden');
    if (notNeeded) notNeeded.textContent = tr(reqs.reason, state.lang);
    state.bundleStatus = { complete: true, ok: true };
    updateStepButtons();
    return;
  }

  section.classList.remove('hidden');
  notNeeded?.classList.add('hidden');
  document.getElementById('uploadSectionTitle').textContent = t('step2.uploadH3');
  document.getElementById('uploadSectionDesc').textContent = tr(reqs.reason, state.lang);
  document.getElementById('uploadModeZip').textContent = t('upload.modeZip');
  document.getElementById('uploadModeSeparate').textContent = t('upload.modeSeparate');
  setUploadMode(state.uploadMode);
  bindZipUploadZone();

  const slotsEl = document.getElementById('uploadSlots');
  if (slotsEl) {
    const separateSlots = (reqs.slots || []).filter(s => s.id !== 'bundle_zip');
    slotsEl.innerHTML = separateSlots.map(s => {
      const accept = (s.accept || ['.zip']).join(',');
      const reqLabel = s.required ? t('upload.required') : t('upload.optional');
      const st = (state.bundleStatus?.slots || []).find(x => x.id === s.id);
      const done = st?.ok ? '✅' : s.required ? '⏳' : '○';
      return `
        <div class="upload-slot ${st?.ok ? 'done' : ''}" data-slot="${s.id}">
          <div class="upload-slot-head">
            <span class="upload-slot-icon">${done}</span>
            <div>
              <strong>${tr(s.label, state.lang)}</strong>
              <span class="upload-slot-badge">${reqLabel}</span>
              <p class="check-detail">${tr(s.hint, state.lang)}</p>
            </div>
          </div>
          <div class="upload-slot-zone" data-slot="${s.id}">
            <input type="file" id="slot-${s.id}" accept="${accept}" hidden>
            <button type="button" class="btn btn-secondary btn-sm slot-browse-btn" data-slot="${s.id}">${t('upload.browse')}</button>
            <span class="upload-slot-file">${st?.filename || ''}</span>
          </div>
        </div>`;
    }).join('');

    separateSlots.forEach(s => {
      const inp = document.getElementById(`slot-${s.id}`);
      const btn = slotsEl.querySelector(`.slot-browse-btn[data-slot="${s.id}"]`);
      if (btn && inp) {
        btn.onclick = (e) => {
          e.preventDefault();
          inp.click();
        };
      }
      if (inp) {
        inp.onchange = () => {
          if (inp.files.length) uploadSlotFile(s.id, inp.files[0]);
          inp.value = '';
        };
      }
    });
  }
  updateStepButtons();
}

async function uploadSlotFile(slot, file) {
  const status = document.getElementById('uploadStatus');
  const inventory = document.getElementById('uploadInventory');
  status.classList.remove('hidden');
  status.textContent = `${t('uploading')} ${file.name}...`;

  const form = new FormData();
  form.append('file', file);
  form.append('slot', slot);
  if (state.uploadBundleId) form.append('bundle_id', state.uploadBundleId);
  if (state.selectedPanel) form.append('panel_id', state.selectedPanel.id);
  if (state.sourceDb) form.append('source_db', state.sourceDb);
  form.append('marzban_mode', 'fresh');

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    state.uploadBundleId = data.bundle_id;
    state.bundleStatus = data.bundle_status;
    state.uploadInfo = data.slot_meta;

    const bs = data.bundle_status || {};
    const ok = bs.complete;
    status.textContent = `${ok ? '✅' : '⚠️'} ${file.name} ${t('uploaded')}`;
    status.style.background = ok ? 'var(--success-bg)' : 'var(--warning-bg)';
    status.style.color = ok ? 'var(--success)' : 'var(--warning)';

    if (bs.analysis) {
      renderUploadInventory({ analysis: bs.analysis });
    } else {
      inventory?.classList.add('hidden');
    }

    applyBundleAnalysis(bs);
    renderUploadSection();
    renderBundleStatus(bs);
    if (state.selectedPanel) await renderPanelPrereqs(state.selectedPanel.id);
    updateStepButtons();
  } catch (e) {
    status.textContent = `❌ ${t('uploadErr')}: ${e.message}`;
    status.style.background = 'var(--error-bg)';
    status.style.color = 'var(--error)';
  }
}

function applyBundleAnalysis(bs) {
  const a = bs?.analysis;
  if (!a) return;
  if (state.selectedPanel?.id === 'marzban') {
    renderMarzbanDetectedSource();
    if (a.detected_source_db) state.sourceDb = a.detected_source_db;
    updateSourceCredentialsVisibility();
  } else if (a.detected_source_db && document.querySelector('#sourceDbGrid .db-card')) {
    selectSourceDb(a.detected_source_db);
  }
  updateSourceCredentialsVisibility();
}

function renderBundleStatus(bs) {
  const el = document.getElementById('uploadBundleStatus');
  if (!el || !bs) return;
  if (bs.upload_mode === 'none' || bs.upload_mode === 'optional' && !state.uploadBundleId) {
    el.classList.add('hidden');
    return;
  }
  const lang = state.lang;
  const rows = (bs.slots || []).map(s => {
    const label = s.label ? tr(s.label, lang) : t(`upload.slot.${s.id}`) || s.id;
    const icon = s.ok ? '✅' : (s.required ? '❌' : '○');
    const via = s.via === 'bundle_zip' ? ` (${t('upload.viaZip')})` : '';
    return `<div class="check-item"><span class="check-icon">${icon}</span><div><div>${label}${via}</div><div class="check-detail">${s.filename || (s.required ? t('upload.missing') : t('upload.optional'))}</div></div></div>`;
  }).join('');
  const head = bs.complete
    ? `<p style="color:var(--success);margin-bottom:8px">✅ ${t('upload.allReady')}</p>`
    : `<p style="color:var(--warning);margin-bottom:8px">⏳ ${t('upload.waitingFiles')}</p>`;
  el.innerHTML = head + rows;
  el.classList.remove('hidden');
}

async function uploadFile(file) {
  return uploadSlotFile('bundle_zip', file);
}

function renderUploadInventory(data) {
  const el = document.getElementById('uploadInventory');
  const a = data.analysis;
  if (!el || !a) return;

  const lang = state.lang;
  const catLabel = (c) => t(`upload.cat.${c}`) || c;
  const fmtSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;

  const badges = Object.entries(a.categories || {}).map(([k, v]) =>
    `<span class="inv-badge">${catLabel(k)}: ${v}</span>`
  ).join('');
  const okBadge = `<span class="inv-badge ${a.backup_ok ? 'ok' : 'warn'}">${a.backup_ok ? t('upload.backupOk') : t('upload.backupIncomplete')}</span>`;

  const rows = (a.inventory || []).map(item => `
    <tr>
      <td>${catLabel(item.category)}</td>
      <td><code>${item.path}</code></td>
      <td>${fmtSize(item.size)}</td>
      <td>${item.pasarguard_note ? `<span class="check-detail">${item.pasarguard_note}</span>` : '—'}</td>
    </tr>`).join('');

  const mapping = (a.env_mapping || []).map(m =>
    `<li><code>${m.from}</code> → <code>${m.to}</code></li>`
  ).join('');

  const warnings = (a.warnings || []).map(w => `<p class="check-detail">⚠️ ${tr(w, lang)}</p>`).join('');
  const missing = (a.missing || []).map(m => `<p class="check-detail">❌ ${tr(m, lang)}</p>`).join('');

  el.innerHTML = `
    <h4>${t('upload.inventoryTitle')}</h4>
    <div class="inv-summary">${okBadge}<span class="inv-badge">${t('upload.fileCount')}: ${a.total_files}</span>${badges}</div>
    ${a.extract_root && a.extract_root !== '.' ? `<p class="check-detail">${t('upload.extractRoot')}: <code>${a.extract_root}</code></p>` : ''}
    ${missing}${warnings}
    ${mapping ? `<div class="inv-map"><strong>${t('upload.envMapping')}</strong><ul>${mapping}</ul></div>` : ''}
    <table>
      <thead><tr><th>${t('upload.colType')}</th><th>${t('upload.colPath')}</th><th>${t('upload.colSize')}</th><th>${t('upload.colPgPath')}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${a.inventory_truncated ? `<p class="check-detail">${t('upload.truncated')}</p>` : ''}`;
  el.classList.remove('hidden');
}
