/* Pre-migration wizard phases: welcome → pg setup → choose → restore|migrate|finish */

Object.assign(state, {
  phase: 'welcome', // welcome | pg | choose | restore | migrate
  panelAccess: null,
  pgDb: 'timescaledb',
  pgSsl: null, // true | false | null
  restoreUploadId: null,
  restoreAnalysis: null,
});

const PHASE_PANELS = {
  welcome: 'panel-welcome',
  pg: 'panel-pg',
  choose: 'panel-choose',
  restore: 'panel-restore',
};

function hideAllMainPanels() {
  document.querySelectorAll('main.main > .panel').forEach(p => p.classList.remove('active'));
}

function showPhase(phase) {
  state.phase = phase;
  if (phase !== 'migrate') {
    hideAllMainPanels();
    const id = PHASE_PANELS[phase];
    if (id) document.getElementById(id)?.classList.add('active');
  }
  if (phase === 'pg') renderPgSetup();
  if (phase === 'choose') applyChooseI18n();
  if (phase === 'restore') setupRestoreUpload();
  renderFlowSteps();
  applyPhaseI18n();
}

function startWizard() {
  showPhase('pg');
}

async function refreshPanelAccess() {
  try {
    const res = await fetch('/api/pasarguard/status');
    state.panelAccess = await res.json();
  } catch (e) {
    console.error(e);
  }
}

function renderFlowSteps() {
  const nav = document.getElementById('stepsNav');
  if (!nav) return;

  let labels;
  let activeIdx = 0;
  if (state.phase === 'welcome') {
    labels = t('stepsSetup') || ['Welcome', 'Setup', 'Next'];
    activeIdx = 0;
  } else if (state.phase === 'pg') {
    labels = t('stepsSetup') || ['Welcome', 'Setup', 'Next'];
    activeIdx = 1;
  } else if (state.phase === 'choose') {
    labels = t('stepsSetup') || ['Welcome', 'Setup', 'Next'];
    activeIdx = 2;
  } else if (state.phase === 'restore') {
    labels = t('stepsRestore') || ['Welcome', 'Setup', 'Next', 'Restore', 'Done'];
    activeIdx = state.restoreAnalysis && document.getElementById('restoreDone')?.classList.contains('hidden') === false
      ? 4 : 3;
  } else if (state.phase === 'migrate') {
    labels = t('steps') || [];
    activeIdx = 3 + (state.currentStep || 1); // welcome,setup,next + migrate steps
    // Map migrate step 1..6 → nav index 3..8
    activeIdx = 2 + (state.currentStep || 1);
  } else {
    labels = t('stepsSetup') || [];
  }

  nav.innerHTML = (labels || []).map((label, i) => {
    const cls = i === activeIdx ? 'active' : (i < activeIdx ? 'done' : '');
    return `<div class="step ${cls}" data-step="${i}"><span class="step-num">${i}</span><span class="step-label">${label}</span></div>`;
  }).join('');
}

function applyPhaseI18n() {
  const set = (id, key) => {
    const el = document.getElementById(id);
    if (el) el.textContent = t(key);
  };
  set('welcomeH2', 'welcome.h2');
  set('welcomeDesc', 'welcome.desc');
  set('welcomeNote', 'welcome.note');
  set('welcomeBackupTip', 'welcome.backupTip');
  set('btnWelcomeStart', 'welcome.start');

  set('pgH2', 'pg.h2');
  set('pgDesc', 'pg.desc');
  set('pgInstalledTitle', 'pg.installedTitle');
  set('pgInstalledDetail', 'pg.installedDetail');
  set('btnPgContinue', 'pg.continue');
  set('btnPgBack', 'pg.back');
  set('btnPgInstall', 'pg.install');
  set('lblPgDb', 'pg.dbLabel');
  set('lblPgSsl', 'pg.sslLabel');
  set('pgSslMustChoose', 'pg.sslMustChoose');
  set('pgSslYes', 'pg.sslYes');
  set('pgSslYesDesc', 'pg.sslYesDesc');
  set('pgSslNo', 'pg.sslNo');
  set('pgSslNoDesc', 'pg.sslNoDesc');
  set('lblPgDomain', 'pg.domain');
  set('pgDomainHint', 'pg.domainHint');
  set('lblPgIp', 'pg.ip');
  set('pgIpHint', 'pg.ipHint');
  set('lblPgSslPort', 'pg.sslPort');
  set('pgSslPortHint', 'pg.sslPortHint');
  set('pgDbMatchTip', 'pg.dbMatchTip');
  set('pgDoneTitle', 'pg.doneTitle');
  set('btnPgDoneNext', 'pg.next');
  bindPgSslButtons();

  applyChooseI18n();

  set('restoreH2', 'restore.h2');
  set('restoreDesc', 'restore.desc');
  set('restoreDbTip', 'restore.tip');
  set('restoreDragText', 'restore.drag');
  set('restoreSelectText', 'restore.select');
  set('btnRestoreConfirm', 'restore.confirm');
  set('btnRestoreBack', 'restore.back');
  set('restoreDoneTitle', 'restore.doneTitle');
  set('restorePanelLink', 'restore.openPanel');
}

function applyChooseI18n() {
  const set = (id, key) => {
    const el = document.getElementById(id);
    if (el) el.textContent = t(key);
  };
  set('chooseH2', 'choose.h2');
  set('chooseDesc', 'choose.desc');
  set('chooseFinish', 'choose.finish');
  set('chooseFinishDesc', 'choose.finishDesc');
  set('chooseRestore', 'choose.restore');
  set('chooseRestoreDesc', 'choose.restoreDesc');
  set('chooseMigrate', 'choose.migrate');
  set('chooseMigrateDesc', 'choose.migrateDesc');
  set('btnChooseBack', 'choose.back');
}

async function renderPgSetup() {
  await loadSystemCheck();
  await refreshPanelAccess();
  const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
  const installedCard = document.getElementById('pgInstalledCard');
  const form = document.getElementById('pgInstallForm');
  const prog = document.getElementById('pgInstallProgress');
  const done = document.getElementById('pgInstallDone');
  prog?.classList.add('hidden');
  done?.classList.add('hidden');

  if (installed) {
    installedCard?.classList.remove('hidden');
    form?.classList.add('hidden');
    const detail = document.getElementById('pgInstalledDetail');
    if (detail) {
      const db = state.systemCheck?.pasarguard_db || state.panelAccess?.db_type || '';
      detail.textContent = `${t('pg.installedDetail')}${db ? ` (${db})` : ''}`;
    }
  } else {
    installedCard?.classList.add('hidden');
    form?.classList.remove('hidden');
    // Always require a fresh SSL choice before showing next fields / install
    state.pgSsl = null;
    bindPgSslButtons();
    renderPgDbGrid();
    selectPgSsl(null);
  }
}

function renderPgDbGrid() {
  const grid = document.getElementById('pgDbGrid');
  if (!grid) return;
  if (!state.pgDb) state.pgDb = 'timescaledb';
  const dbs = (state.pasarguardInstallDbs?.length
    ? state.pasarguardInstallDbs
    : ['sqlite', 'mysql', 'mariadb', 'postgresql', 'timescaledb']);
  grid.innerHTML = dbs.map(db => {
    const name = (typeof dbDisplayName === 'function' ? dbDisplayName(db) : db);
    const selected = state.pgDb === db ? 'selected' : '';
    const rec = db === 'timescaledb' ? 'recommended' : '';
    return `<button type="button" class="db-card ${selected} ${rec}" data-db="${db}"><h4>${name}</h4></button>`;
  }).join('');

  if (!grid.dataset.bound) {
    grid.dataset.bound = '1';
    grid.addEventListener('click', (e) => {
      const btn = e.target.closest('.db-card[data-db]');
      if (!btn) return;
      e.preventDefault();
      selectPgDb(btn.dataset.db);
    });
  }
}

function selectPgDb(db) {
  if (!db) return;
  state.pgDb = db;
  renderPgDbGrid();
  const block = document.getElementById('pgInstallBlock');
  if (block) block.classList.add('hidden');
}

function bindPgSslButtons() {
  const yes = document.getElementById('btnPgSslYes');
  const no = document.getElementById('btnPgSslNo');
  if (yes && !yes.dataset.bound) {
    yes.dataset.bound = '1';
    yes.addEventListener('click', (e) => { e.preventDefault(); selectPgSsl(true); });
  }
  if (no && !no.dataset.bound) {
    no.dataset.bound = '1';
    no.addEventListener('click', (e) => { e.preventDefault(); selectPgSsl(false); });
  }
}

function selectPgSsl(yes) {
  state.pgSsl = yes;
  document.querySelectorAll('#pgSslGrid .choice-card').forEach(el => {
    const v = el.dataset.ssl === 'yes';
    el.classList.toggle('active', yes !== null && yes !== undefined && v === !!yes);
  });

  const after = document.getElementById('pgAfterSsl');
  const yesFields = document.getElementById('pgSslYesFields');
  const noHint = document.getElementById('pgSslNoHint');
  const must = document.getElementById('pgSslMustChoose');

  // Until Yes/No is chosen — hide everything after SSL
  if (yes !== true && yes !== false) {
    after?.classList.add('hidden');
    yesFields?.classList.add('hidden');
    noHint?.classList.add('hidden');
    if (must) must.classList.remove('hidden');
    return;
  }

  if (must) must.classList.add('hidden');
  after?.classList.remove('hidden');

  if (yes === true) {
    yesFields?.classList.remove('hidden');
    noHint?.classList.add('hidden');
    const ipEl = document.getElementById('pgIp');
    if (ipEl && !ipEl.value) ipEl.value = (state.serverIp || '').split(':')[0];
    const portEl = document.getElementById('pgSslHttpPort');
    if (portEl && !portEl.value) portEl.value = '80';
  } else {
    yesFields?.classList.add('hidden');
    noHint?.classList.remove('hidden');
    const access = state.panelAccess || {};
    const notes = (access.no_ssl_notes && access.no_ssl_notes[state.lang]) || [];
    if (noHint) {
      noHint.innerHTML = notes.map(n => `<p>${n}</p>`).join('')
        || `<p>ssh -L 8000:localhost:8000 user@${state.serverIp}</p><p>http://localhost:8000/dashboard/</p>`;
    }
  }
  const block = document.getElementById('pgInstallBlock');
  if (block) block.classList.add('hidden');
}

function validatePgForm() {
  if (!state.pgDb) return t('pg.pickDb');
  if (state.pgSsl === null || state.pgSsl === undefined) return t('pg.pickSsl');
  if (state.pgSsl === true) {
    const domain = document.getElementById('pgDomain')?.value?.trim();
    const ip = document.getElementById('pgIp')?.value?.trim();
    if (!domain && !ip) return t('pg.needDomainOrIp');
    const port = document.getElementById('pgSslHttpPort')?.value?.trim() || '80';
    const n = parseInt(port, 10);
    if (!Number.isFinite(n) || n < 1 || n > 65535) return t('pg.badSslPort');
  }
  return null;
}

async function startPgInstall() {
  const block = validatePgForm();
  const blockEl = document.getElementById('pgInstallBlock');
  if (block) {
    if (blockEl) { blockEl.textContent = block; blockEl.classList.remove('hidden'); }
    return;
  }
  blockEl?.classList.add('hidden');

  document.getElementById('pgInstallForm')?.classList.add('hidden');
  document.getElementById('pgInstalledCard')?.classList.add('hidden');
  document.getElementById('pgInstallProgress')?.classList.remove('hidden');
  document.getElementById('pgInstallDone')?.classList.add('hidden');
  document.getElementById('pgLogTerminal')?.classList.add('hidden');

  const domain = document.getElementById('pgDomain')?.value?.trim() || null;
  const ip = document.getElementById('pgIp')?.value?.trim() || null;
  const sslPort = parseInt(document.getElementById('pgSslHttpPort')?.value || '80', 10) || 80;
  const body = {
    database: state.pgDb,
    ssl: !!state.pgSsl,
    domain: state.pgSsl && domain ? domain : null,
    ip: state.pgSsl && !domain ? ip : null,
    ssl_http_port: sslPort,
    wipe_volumes: false,
    force: false,
  };

  const status = document.getElementById('pgStatusMsg');
  if (status) status.textContent = t('pg.installing');

  try {
    const res = await fetch('/api/pasarguard/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail;
      const msg = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map(d => d.msg || d).join(', ')
        : (detail?.errors ? detail.errors.join(', ') : null)
        || data.message || res.statusText || 'Install request failed';
      throw new Error(msg);
    }
    pollPgInstall(data.job_id);
  } catch (e) {
    showPgInstallError(e.message || String(e));
  }
}

function showPgInstallError(msg) {
  document.getElementById('pgInstallProgress')?.classList.add('hidden');
  document.getElementById('pgInstallDone')?.classList.add('hidden');
  document.getElementById('pgInstallForm')?.classList.remove('hidden');
  const blockEl = document.getElementById('pgInstallBlock');
  if (blockEl) {
    blockEl.textContent = msg;
    blockEl.classList.remove('hidden');
  }
  const status = document.getElementById('pgStatusMsg');
  if (status) status.textContent = '';
}

async function pollPgInstall(jobId) {
  const fill = document.getElementById('pgProgressFill');
  const text = document.getElementById('pgProgressText');
  const status = document.getElementById('pgStatusMsg');
  const term = document.getElementById('pgLogTerminal');

  const tick = async () => {
    try {
      const res = await fetch(`/api/pasarguard/install/${jobId}`);
      const job = await res.json();
      if (fill) fill.style.width = `${job.progress || 0}%`;
      if (text) text.textContent = `${job.progress || 0}%`;
      if (status) status.textContent = job.status === 'error'
        ? ''
        : (job.message || t('pg.installing'));

      if (job.status === 'success') {
        await loadSystemCheck();
        await refreshPanelAccess();
        showPgInstallDone(job.result || state.panelAccess);
        return;
      }
      if (job.status === 'error') {
        showPgInstallError(job.message || job.result?.error || 'Installation failed');
        return;
      }
      setTimeout(tick, 1500);
    } catch (e) {
      setTimeout(tick, 2500);
    }
  };
  tick();
}

function showPgInstallDone(result) {
  document.getElementById('pgInstallProgress')?.classList.add('hidden');
  const done = document.getElementById('pgInstallDone');
  done?.classList.remove('hidden');
  const notes = document.getElementById('pgDoneNotes');
  if (!notes) return;
  const lang = state.lang;
  const access = result || state.panelAccess || {};
  const owner = (access.owner_notes && access.owner_notes[lang]) || [];
  const noSsl = !access.ssl ? ((access.no_ssl_notes && access.no_ssl_notes[lang]) || []) : [];
  notes.innerHTML = [
    ...owner.map(n => `<p>• ${n}</p>`),
    ...noSsl.map(n => `<p>• ${n}</p>`),
  ].join('');
}

async function choosePath(path) {
  if (path === 'finish') {
    await refreshPanelAccess();
    const access = state.panelAccess || {};
    if (!access.ssl) {
      // Show notes then open localhost-oriented URL
      const url = access.localhost_url || access.panel_url;
      if (url) window.open(url, '_blank');
      alert((access.no_ssl_notes?.[state.lang] || []).join('\n'));
      return;
    }
    const url = access.public_url || access.panel_url;
    if (url) window.location.href = url;
    return;
  }
  if (path === 'restore') {
    showPhase('restore');
    return;
  }
  if (path === 'migrate') {
    state.phase = 'migrate';
    state.currentStep = 1;
    hideAllMainPanels();
    document.getElementById('step1')?.classList.add('active');
    renderPanels();
    renderFlowSteps();
    updateStepButtons();
  }
}

function setupRestoreUpload() {
  const zone = document.getElementById('restoreUploadZone');
  const input = document.getElementById('restoreFileInput');
  if (!zone || zone.dataset.ready) return;
  zone.dataset.ready = '1';
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const f = e.dataTransfer.files?.[0];
    if (f) uploadRestoreZip(f);
  });
  input.addEventListener('change', () => {
    const f = input.files?.[0];
    if (f) uploadRestoreZip(f);
  });
}

async function uploadRestoreZip(file) {
  const status = document.getElementById('restoreUploadStatus');
  const btn = document.getElementById('btnRestoreConfirm');
  status.classList.remove('hidden');
  status.textContent = t('restore.analyzing');
  btn.disabled = true;
  state.restoreAnalysis = null;

  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'upload failed');
    state.restoreUploadId = data.upload_id;
    const ares = await fetch(`/api/pasarguard/restore/analyze/${data.upload_id}`);
    const analysis = await ares.json();
    if (!ares.ok) throw new Error(analysis.detail || 'analyze failed');
    state.restoreAnalysis = analysis;
    renderRestoreAnalysis(analysis);
    btn.disabled = !analysis.ok;
    status.textContent = analysis.ok ? `✅ ${file.name}` : `⚠️ ${file.name}`;
  } catch (e) {
    status.textContent = `❌ ${e.message}`;
    btn.disabled = true;
  }
}

function renderRestoreAnalysis(a) {
  const card = document.getElementById('restoreAnalysis');
  const warn = document.getElementById('restoreWarnings');
  if (!card) return;
  card.classList.remove('hidden');
  card.innerHTML = `
    <div class="summary-row"><span class="summary-label">Backup DB</span><span>${a.backup_db || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">Installed DB</span><span>${a.installed_db || '—'}</span></div>
    <div class="summary-row"><span class="summary-label">Match</span><span>${a.db_match === true ? '✅' : a.db_match === false ? '❌' : '—'}</span></div>
    <div class="summary-row"><span class="summary-label">Layout</span><span>${a.layout || '—'}</span></div>
    ${a.timescaledb_versions?.length ? `<div class="summary-row"><span class="summary-label">TimescaleDB</span><span>${a.timescaledb_versions.join(', ')}</span></div>` : ''}
  `;
  if (warn) {
    const lang = state.lang;
    const items = (a.warnings || []).map(w => `<p>⚠️ ${tr(w, lang)}</p>`).join('');
    warn.innerHTML = items;
    warn.classList.toggle('hidden', !items);
  }
}

async function startRestore() {
  if (!state.restoreUploadId || !state.restoreAnalysis?.ok) {
    const el = document.getElementById('restoreBlock');
    if (el) {
      el.textContent = t('restore.confirmNeeded');
      el.classList.remove('hidden');
    }
    return;
  }
  document.getElementById('restoreProgress')?.classList.remove('hidden');
  document.getElementById('restoreDone')?.classList.add('hidden');
  document.getElementById('btnRestoreConfirm').disabled = true;

  try {
    const res = await fetch('/api/pasarguard/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upload_id: state.restoreUploadId,
        confirmed: true,
        force: false,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
    pollRestore(data.job_id);
  } catch (e) {
    document.getElementById('restoreStatusMsg').textContent = e.message;
    document.getElementById('btnRestoreConfirm').disabled = false;
  }
}

async function pollRestore(jobId) {
  const fill = document.getElementById('restoreProgressFill');
  const text = document.getElementById('restoreProgressText');
  const status = document.getElementById('restoreStatusMsg');
  const term = document.getElementById('restoreLogTerminal');
  let lastLen = 0;
  const tick = async () => {
    try {
      const res = await fetch(`/api/pasarguard/restore/${jobId}`);
      const job = await res.json();
      if (fill) fill.style.width = `${job.progress || 0}%`;
      if (text) text.textContent = `${job.progress || 0}%`;
      if (status) status.textContent = job.message || t('restore.restoring');
      if (term && job.logs?.length > lastLen) {
        term.textContent = job.logs.join('\n');
        term.scrollTop = term.scrollHeight;
        lastLen = job.logs.length;
      }
      if (job.status === 'success') {
        showRestoreDone(job.result || {});
        return;
      }
      if (job.status === 'error') {
        if (status) status.textContent = job.message || 'Error';
        document.getElementById('btnRestoreConfirm').disabled = false;
        return;
      }
      setTimeout(tick, 1500);
    } catch (e) {
      setTimeout(tick, 2500);
    }
  };
  tick();
}

function showRestoreDone(result) {
  document.getElementById('restoreProgress')?.classList.add('hidden');
  document.getElementById('restoreDone')?.classList.remove('hidden');
  const link = document.getElementById('restorePanelLink');
  const url = result.public_url || result.panel_url || '#';
  if (link) {
    link.href = url;
    link.textContent = t('restore.openPanel');
  }
  const notes = document.getElementById('restoreAccessNotes');
  if (notes) {
    const lang = state.lang;
    const owner = (result.owner_notes && result.owner_notes[lang]) || [];
    const noSsl = !result.ssl ? ((result.no_ssl_notes && result.no_ssl_notes[lang]) || []) : [];
    notes.innerHTML = [...owner, ...noSsl].map(n => `<p>• ${n}</p>`).join('');
  }
  renderFlowSteps();
}

// Expose for inline handlers / debugging
window.showPhase = showPhase;
window.startWizard = startWizard;
window.selectPgDb = selectPgDb;
window.selectPgSsl = selectPgSsl;
window.startPgInstall = startPgInstall;
window.choosePath = choosePath;
window.startRestore = startRestore;
