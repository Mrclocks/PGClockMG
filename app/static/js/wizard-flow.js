/* Pre-migration wizard phases: welcome(goal) → pg(if needed) → install|change_db|migrate */

Object.assign(state, {
  phase: 'welcome', // welcome | pg | choose | restore | migrate
  wizardGoal: null, // install | change_db | migrate
  panelAccess: null,
  pgDb: 'timescaledb',
  pgSsl: null, // true | false | null
  pgDomain: '',
  pgIp: '',
  restoreUploadId: null,
  restoreAnalysis: null,
  restoreStage: 'form', // form | running | error | done
  pendingLoginUrl: null,
});

let _restorePollTimer = null;
let _pgInstallPollTimer = null;

function stopRestorePoll() {
  if (_restorePollTimer) {
    clearTimeout(_restorePollTimer);
    _restorePollTimer = null;
  }
}

function stopPgInstallPoll() {
  if (_pgInstallPollTimer) {
    clearTimeout(_pgInstallPollTimer);
    _pgInstallPollTimer = null;
  }
}

/** Append-only log update (avoids rewriting huge textContent every poll). */
function appendJobLogs(term, logs, cursor) {
  if (!term || !Array.isArray(logs) || logs.length <= cursor.lastLen) return cursor.lastLen;
  const chunk = logs.slice(cursor.lastLen).join('\n');
  const prefix = cursor.lastLen > 0 && term.textContent ? '\n' : '';
  term.appendChild(document.createTextNode(prefix + chunk));
  cursor.lastLen = logs.length;
  term.scrollTop = term.scrollHeight;
  return cursor.lastLen;
}

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function copyText(codeOrId, raw) {
  const el = typeof codeOrId === 'string' ? document.getElementById(codeOrId) : null;
  const text = raw != null ? String(raw) : (el?.textContent || codeOrId || '');
  try {
    await navigator.clipboard.writeText(text.trim());
    const btn = (typeof event !== 'undefined' && event?.currentTarget) || null;
    if (btn && btn.classList) {
      const prev = btn.textContent;
      btn.textContent = t('copied');
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = prev; btn.classList.remove('copied'); }, 1600);
    }
  } catch (e) {
    alert(text);
  }
}

function renderGuideSections(container, access) {
  if (!container) return;
  const lang = state.lang || 'fa';
  const sections = (access?.guide && access.guide[lang]) || [];
  if (!sections.length) {
    const owner = (access?.owner_notes && access.owner_notes[lang]) || [];
    const noSsl = !access?.ssl ? ((access?.no_ssl_notes && access.no_ssl_notes[lang]) || []) : [];
    container.innerHTML = [...owner, ...noSsl].map(n => `<p class="guide-line">${escapeHtml(n)}</p>`).join('');
    return;
  }
  container.innerHTML = sections.map((sec, si) => {
    const items = (sec.items || []).map((it, ii) => {
      const copy = it.copy;
      if (copy) {
        const id = `guide-${si}-${ii}`;
        return `<div class="guide-item">
          <p class="guide-line">${escapeHtml(it.text || '')}</p>
          <div class="install-cmd-row">
            <div class="install-cmd-box"><code id="${id}">${escapeHtml(copy)}</code></div>
            <button type="button" class="btn btn-copy" onclick="copyText('${id}')">${escapeHtml(t('copy'))}</button>
          </div>
        </div>`;
      }
      return `<p class="guide-line">${escapeHtml(it.text || '')}</p>`;
    }).join('');
    return `<section class="guide-block"><h4 class="guide-title">${escapeHtml(sec.title || '')}</h4>${items}</section>`;
  }).join('');
}

function resolveLoginUrl(access) {
  const a = access || state.panelAccess || {};
  const host = (state.pgDomain || a.domain || state.pgIp || a.host || a.ip || '').trim();
  const port = a.port || '8000';
  const root = (a.root_path && a.root_path !== '/' ? a.root_path : '') || '';
  if (host && host !== '127.0.0.1' && host !== 'localhost') {
    const path = `${root}/dashboard/`.replace(/\/{2,}/g, '/');
    const p = path.startsWith('/') ? path : `/${path}`;
    return `https://${host}:${port}${p}`;
  }
  return a.login_url || a.public_url || a.localhost_url || a.panel_url || '';
}

function openFinishModal(loginUrl) {
  state.pendingLoginUrl = loginUrl;
  const modal = document.getElementById('finishModal');
  if (!modal) {
    goToPanel(loginUrl);
    return;
  }
  document.getElementById('finishModalTitle').textContent = t('finishModal.title');
  document.getElementById('finishModalDesc').textContent = t('finishModal.desc');
  document.getElementById('btnFinishCancel').textContent = t('finishModal.cancel');
  document.getElementById('btnFinishUninstall').textContent = t('finishModal.uninstall');
  document.getElementById('btnFinishContinue').textContent = t('finishModal.continue');
  modal.classList.remove('hidden');
}

function closeFinishModal() {
  document.getElementById('finishModal')?.classList.add('hidden');
}

function goToPanel(url) {
  const u = url || state.pendingLoginUrl || resolveLoginUrl();
  if (u) window.open(u, '_blank');
}

function bindFinishModal() {
  const modal = document.getElementById('finishModal');
  if (!modal || modal.dataset.bound) return;
  modal.dataset.bound = '1';
  document.getElementById('btnFinishCancel')?.addEventListener('click', () => closeFinishModal());
  document.getElementById('btnFinishContinue')?.addEventListener('click', () => {
    closeFinishModal();
    goToPanel();
  });
  document.getElementById('btnFinishUninstall')?.addEventListener('click', async () => {
    closeFinishModal();
    await uninstallWizard(true);
    goToPanel();
  });
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeFinishModal();
  });
}

async function uninstallWizard(skipConfirm) {
  const errEls = [
    document.getElementById('restoreUninstallErr'),
    document.getElementById('migrateUninstallErr'),
  ];
  errEls.forEach(el => { if (el) { el.textContent = ''; el.classList.add('hidden'); } });

  if (!skipConfirm && !confirm(t('uninstall.confirm'))) return;
  try {
    const res = await fetch('/api/self-uninstall', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (typeof data.detail === 'string' ? data.detail : null)
        || data.message
        || t('uninstall.failed');
      throw new Error(msg);
    }
    if (!skipConfirm) alert(t('uninstall.scheduled'));
  } catch (e) {
    const msg = e.message || String(e) || t('uninstall.failed');
    errEls.forEach(el => {
      if (!el) return;
      el.textContent = msg;
      el.classList.remove('hidden');
    });
    if (skipConfirm) alert(msg);
  }
}

window.copyText = copyText;
window.uninstallWizard = uninstallWizard;

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
  if (phase === 'restore') {
    state.restoreStage = state.restoreStage === 'done' ? 'done' : 'form';
    if (state.restoreStage === 'form') setRestoreStage('form');
    setupRestoreUpload();
  }
  renderFlowSteps();
  applyPhaseI18n();
}

/** User picks goal on welcome: install | change_db | migrate */
async function startWizardGoal(goal) {
  state.wizardGoal = goal;
  await loadSystemCheck();
  await refreshPanelAccess();
  const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
  if (installed) {
    await continueAfterPgReady();
  } else {
    showPhase('pg');
  }
}

/** Legacy entry — keep for any leftover callers */
function startWizard() {
  startWizardGoal(state.wizardGoal || 'install');
}

/** After PG is ready (already installed or just installed), jump to chosen goal. */
async function continueAfterPgReady() {
  const goal = state.wizardGoal || 'install';
  if (goal === 'install') {
    await choosePath('finish');
    return;
  }
  if (goal === 'change_db') {
    showPhase('restore');
    return;
  }
  if (goal === 'migrate') {
    await choosePath('migrate');
    return;
  }
  showPhase('choose');
}

function backFromRestore() {
  if (state.wizardGoal === 'change_db') {
    showPhase('welcome');
    return;
  }
  showPhase(state.wizardGoal ? 'welcome' : 'choose');
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

  const goal = state.wizardGoal;
  let labels;
  let activeIdx = 0;

  if (goal === 'migrate' || state.phase === 'migrate') {
    labels = t('stepsMigrate') || t('steps') || [];
    if (state.phase === 'welcome') activeIdx = 0;
    else if (state.phase === 'pg') activeIdx = 1;
    else activeIdx = 1 + (state.currentStep || 1);
  } else if (goal === 'change_db' || state.phase === 'restore') {
    labels = t('stepsChangeDb') || t('stepsRestore') || [];
    if (state.phase === 'welcome') activeIdx = 0;
    else if (state.phase === 'pg') activeIdx = 1;
    else if (state.restoreStage === 'done') activeIdx = 4;
    else if (state.restoreStage === 'running' || state.restoreStage === 'error') activeIdx = 3;
    else activeIdx = 2;
  } else if (goal === 'install') {
    labels = t('stepsInstall') || t('stepsSetup') || [];
    if (state.phase === 'welcome') activeIdx = 0;
    else if (state.phase === 'pg') activeIdx = 1;
    else activeIdx = 2;
  } else if (state.phase === 'welcome') {
    labels = t('stepsSetup') || ['Welcome', 'Setup', 'Next'];
    activeIdx = 0;
  } else if (state.phase === 'pg') {
    labels = t('stepsSetup') || ['Welcome', 'Setup', 'Next'];
    activeIdx = 1;
  } else if (state.phase === 'choose') {
    labels = t('stepsSetup') || ['Welcome', 'Setup', 'Next'];
    activeIdx = 2;
  } else if (state.phase === 'restore') {
    labels = t('stepsChangeDb') || t('stepsRestore') || [];
    if (state.restoreStage === 'done') activeIdx = 4;
    else if (state.restoreStage === 'running' || state.restoreStage === 'error') activeIdx = 3;
    else activeIdx = 2;
  } else if (state.phase === 'migrate') {
    labels = t('stepsMigrate') || t('steps') || [];
    activeIdx = 1 + (state.currentStep || 1);
  } else {
    labels = t('stepsSetup') || [];
  }

  const list = labels || [];
  const checkSvg = (typeof icon === 'function')
    ? icon('check')
    : '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 12.5l2.5 2.5L16 9"/></svg>';
  const parts = [];
  list.forEach((label, i) => {
    if (i > 0) {
      parts.push(`<div class="step-rail ${i <= activeIdx ? 'done' : ''}" aria-hidden="true"></div>`);
    }
    const cls = i === activeIdx ? 'active' : (i < activeIdx ? 'done' : '');
    const numInner = i < activeIdx ? checkSvg : String(i + 1);
    parts.push(
      `<div class="step ${cls}" data-step="${i}">`
      + `<span class="step-num">${numInner}</span>`
      + `<span class="step-label">${escapeHtml(label)}</span>`
      + `</div>`
    );
  });
  nav.innerHTML = parts.join('');
}

function applyPhaseI18n() {
  const set = (id, key) => {
    const el = document.getElementById(id);
    if (el) el.textContent = t(key);
  };
  set('welcomeH2', 'welcome.h2');
  set('welcomeDesc', 'welcome.desc');
  set('welcomeNote', 'welcome.note');
  set('welcomeGoalHint', 'welcome.goalHint');
  set('welcomeGoalInstall', 'welcome.goalInstall');
  set('welcomeGoalInstallDesc', 'welcome.goalInstallDesc');
  set('welcomeGoalChangeDb', 'welcome.goalChangeDb');
  set('welcomeGoalChangeDbDesc', 'welcome.goalChangeDbDesc');
  set('welcomeGoalMigrate', 'welcome.goalMigrate');
  set('welcomeGoalMigrateDesc', 'welcome.goalMigrateDesc');
  set('welcomeBackupTip', 'welcome.backupTip');

  set('pgH2', 'pg.h2');
  set('pgDesc', 'pg.desc');
  set('pgInstalledTitle', 'pg.installedTitle');
  set('pgInstalledDetail', 'pg.installedDetail');
  set('btnPgContinue', 'pg.continue');
  set('btnPgBack', 'pg.back');
  set('btnPgInstalledBack', 'pg.back');
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
  set('pgDbMatchTip', state.wizardGoal === 'change_db' ? 'pg.dbMatchTipChange' : 'pg.dbMatchTip');
  set('pgDoneTitle', 'pg.doneTitle');
  set('btnPgDoneNext', 'pg.next');
  bindPgSslButtons();

  applyChooseI18n();

  const restoreH2Key = state.wizardGoal === 'change_db' ? 'restore.h2ChangeDb' : 'restore.h2';
  const restoreDescKey = state.wizardGoal === 'change_db' ? 'restore.descChangeDb' : 'restore.desc';
  set('restoreH2', restoreH2Key);
  set('restoreDesc', restoreDescKey);
  set('restoreDbTipText', 'restore.tip');
  set('restoreDragText', 'restore.drag');
  set('restoreSelectText', 'restore.select');
  set('btnRestoreConfirm', 'restore.confirm');
  set('btnRestoreBack', 'restore.back');
  set('restoreDoneTitle', 'restore.doneTitle');
  set('restorePanelLink', 'restore.openPanel');
  set('restoreRunningTitle', 'restore.runningTitle');
  set('restoreRunningDesc', 'restore.runningDesc');
  set('restoreErrorTitle', 'restore.errorTitle');
  set('restoreErrorDetailToggle', 'restore.errorDetail');
  set('btnRestoreErrorBack', 'restore.back');
  set('btnRestoreRetry', 'restore.retry');
  set('restoreConvertNoteText', 'restore.autoConvertNote');
  set('btnCopyRestorePath', 'copy');
  set('restoreUninstallTitle', 'uninstall.title');
  set('restoreUninstallTip', 'uninstall.tip');
  set('btnUninstallRestore', 'uninstall.button');
  set('migrateUninstallTitle', 'uninstall.title');
  set('migrateUninstallTip', 'uninstall.tip');
  set('btnUninstallMigrate', 'uninstall.button');
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
    const rec = db === 'timescaledb'
      ? `<span class="db-badge">${t('dbRecommended')}</span>`
      : '';
    return `<button type="button" class="db-card ${selected}" data-db="${db}"><h4>${name}</h4>${rec}</button>`;
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
    el.classList.toggle('selected', yes !== null && yes !== undefined && v === !!yes);
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
  const term = document.getElementById('pgLogTerminal');
  if (term) {
    term.classList.remove('hidden');
    term.textContent = '';
  }

  const domain = document.getElementById('pgDomain')?.value?.trim() || null;
  const ip = document.getElementById('pgIp')?.value?.trim() || null;
  state.pgDomain = domain || '';
  state.pgIp = ip || '';
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
  stopPgInstallPoll();
  const fill = document.getElementById('pgProgressFill');
  const text = document.getElementById('pgProgressText');
  const status = document.getElementById('pgStatusMsg');
  const term = document.getElementById('pgLogTerminal');
  if (term) term.textContent = '';
  const cursor = { lastLen: 0 };

  const tick = async () => {
    try {
      const res = await fetch(`/api/pasarguard/install/${jobId}`);
      const job = await res.json();
      if (fill) fill.style.width = `${job.progress || 0}%`;
      if (text) text.textContent = `${job.progress || 0}%`;
      if (status) status.textContent = job.status === 'error'
        ? ''
        : (job.message || t('pg.installing'));
      if (term) {
        term.classList.remove('hidden');
        appendJobLogs(term, job.logs, cursor);
      }

      if (job.status === 'success') {
        stopPgInstallPoll();
        await loadSystemCheck();
        await refreshPanelAccess();
        showPgInstallDone(job.result || state.panelAccess);
        return;
      }
      if (job.status === 'error') {
        stopPgInstallPoll();
        showPgInstallError(job.message || job.result?.error || 'Installation failed');
        return;
      }
      _pgInstallPollTimer = setTimeout(tick, 1000);
    } catch (e) {
      _pgInstallPollTimer = setTimeout(tick, 2000);
    }
  };
  tick();
}

function showPgInstallDone(result) {
  document.getElementById('pgInstallProgress')?.classList.add('hidden');
  const done = document.getElementById('pgInstallDone');
  done?.classList.remove('hidden');
  const access = result || state.panelAccess || {};
  state.panelAccess = access;
  renderGuideSections(document.getElementById('pgDoneNotes'), access);
}

async function choosePath(path) {
  if (path === 'finish') {
    state.wizardGoal = state.wizardGoal || 'install';
    bindFinishModal();
    await refreshPanelAccess();
    const url = resolveLoginUrl(state.panelAccess);
    openFinishModal(url);
    renderFlowSteps();
    return;
  }
  if (path === 'restore') {
    state.wizardGoal = 'change_db';
    showPhase('restore');
    return;
  }
  if (path === 'migrate') {
    state.wizardGoal = 'migrate';
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
    status.textContent = analysis.ok ? file.name : file.name;
    status.classList.toggle('is-ok', !!analysis.ok);
    status.classList.toggle('is-warn', !analysis.ok);
  } catch (e) {
    status.textContent = e.message;
    status.classList.add('is-warn');
    btn.disabled = true;
  }
}

function setRestoreStage(stage) {
  state.restoreStage = stage;
  document.getElementById('restoreFormStage')?.classList.toggle('hidden', stage !== 'form');
  document.getElementById('restoreRunningStage')?.classList.toggle('hidden', stage !== 'running');
  document.getElementById('restoreErrorStage')?.classList.toggle('hidden', stage !== 'error');
  document.getElementById('restoreDone')?.classList.toggle('hidden', stage !== 'done');
  renderFlowSteps();
}

function resetRestoreForm() {
  stopRestorePoll();
  setRestoreStage('form');
  document.getElementById('btnRestoreConfirm').disabled = !state.restoreAnalysis?.ok;
  applyPhaseI18n();
}

function renderRestoreAnalysis(a) {
  const card = document.getElementById('restoreAnalysis');
  const warn = document.getElementById('restoreWarnings');
  if (!card) return;
  card.classList.remove('hidden');
  const s = t('restore.summary') || {};
  card.innerHTML = `
    <div class="summary-row"><span class="summary-label">${escapeHtml(s.backupDb || 'Backup DB')}</span><span>${escapeHtml(a.backup_db || '—')}</span></div>
    <div class="summary-row"><span class="summary-label">${escapeHtml(s.installedDb || 'Installed DB')}</span><span>${escapeHtml(a.installed_db || '—')}</span></div>
    <div class="summary-row"><span class="summary-label">${escapeHtml(s.match || 'Match')}</span><span class="status-inline">${a.db_match === true ? statusIcon('ok') : a.db_match === false ? statusIcon(false) : '—'}</span></div>
    <div class="summary-row"><span class="summary-label">${escapeHtml(s.layout || 'Layout')}</span><span>${escapeHtml(a.layout || '—')}</span></div>
    ${a.timescaledb_versions?.length ? `<div class="summary-row"><span class="summary-label">TimescaleDB</span><span>${escapeHtml(a.timescaledb_versions.join(', '))}</span></div>` : ''}
  `;
  if (warn) {
    const lang = state.lang;
    const items = (a.warnings || []).map(w => `<p class="warn-line">${statusIcon('warn')}<span>${escapeHtml(tr(w, lang))}</span></p>`).join('');
    warn.innerHTML = items;
    warn.classList.toggle('hidden', !items);
  }

  const note = document.getElementById('restoreConvertNote');
  const needsConvert = a.backup_db && a.installed_db && a.backup_db !== a.installed_db
    && !(
      (['mysql', 'mariadb'].includes(a.backup_db) && ['mysql', 'mariadb'].includes(a.installed_db))
      || (['postgresql', 'timescaledb'].includes(a.backup_db) && ['postgresql', 'timescaledb'].includes(a.installed_db))
    );
  if (note) {
    note.classList.toggle('hidden', !needsConvert);
    const noteText = document.getElementById('restoreConvertNoteText');
    if (noteText) noteText.textContent = t('restore.autoConvertNote');
  }

  updateRestoreConfirmEnabled();
}

function updateRestoreConfirmEnabled() {
  const btn = document.getElementById('btnRestoreConfirm');
  const a = state.restoreAnalysis;
  if (!btn || !a) return;
  btn.disabled = !a.ok;
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

  stopRestorePoll();
  setRestoreStage('running');
  applyPhaseI18n();
  const fill = document.getElementById('restoreProgressFill');
  const text = document.getElementById('restoreProgressText');
  const status = document.getElementById('restoreStatusMsg');
  const term = document.getElementById('restoreLogTerminal');
  if (fill) fill.style.width = '0%';
  if (text) text.textContent = '0%';
  if (status) status.textContent = t('restore.restoring');
  if (term) term.textContent = '';

  try {
    const res = await fetch('/api/pasarguard/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upload_id: state.restoreUploadId,
        confirmed: true,
        force: false,
        // Destination is always the installed PasarGuard DB
        target_db: state.restoreAnalysis.installed_db || undefined,
        accept_experimental: true,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : (data.detail?.msg || JSON.stringify(data)));
    pollRestore(data.job_id);
  } catch (e) {
    showRestoreError({ fa: e.message, en: e.message, causes_fa: [], detail: e.message });
  }
}

async function pollRestore(jobId) {
  stopRestorePoll();
  const fill = document.getElementById('restoreProgressFill');
  const text = document.getElementById('restoreProgressText');
  const status = document.getElementById('restoreStatusMsg');
  const term = document.getElementById('restoreLogTerminal');
  const cursor = { lastLen: 0 };

  const tick = async () => {
    try {
      const res = await fetch(`/api/pasarguard/restore/${jobId}`);
      const job = await res.json();
      if (fill) fill.style.width = `${job.progress || 0}%`;
      if (text) text.textContent = `${job.progress || 0}%`;
      if (status) status.textContent = job.message || t('restore.restoring');
      appendJobLogs(term, job.logs, cursor);

      if (job.status === 'success') {
        stopRestorePoll();
        showRestoreDone(job.result || {});
        return;
      }
      if (job.status === 'error') {
        stopRestorePoll();
        const explain = job.result?.error_explain || {
          fa: job.message,
          en: job.message,
          causes_fa: [],
          detail: job.result?.error || job.message,
        };
        showRestoreError(explain, job.logs);
        return;
      }
      _restorePollTimer = setTimeout(tick, 900);
    } catch (e) {
      _restorePollTimer = setTimeout(tick, 1800);
    }
  };
  tick();
}

function showRestoreError(explain, logs) {
  setRestoreStage('error');
  applyPhaseI18n();
  const lang = state.lang || 'fa';
  const msg = (lang === 'fa' ? explain.fa : lang === 'ru' ? explain.ru : explain.en)
    || explain.fa || explain.en || t('restore.errorTitle');
  const msgEl = document.getElementById('restoreErrorMsg');
  if (msgEl) msgEl.textContent = msg;

  const causesBox = document.getElementById('restoreErrorCauses');
  const causes = explain.causes_fa || [];
  if (causesBox) {
    if (causes.length && (lang === 'fa' || !explain.causes_en)) {
      causesBox.innerHTML = `<h4>${escapeHtml(t('restore.causesTitle'))}</h4><ul>${
        causes.map(c => `<li>${escapeHtml(c)}</li>`).join('')
      }</ul>`;
      causesBox.classList.remove('hidden');
    } else {
      causesBox.classList.add('hidden');
      causesBox.innerHTML = '';
    }
  }

  const detail = document.getElementById('restoreErrorDetail');
  if (detail) {
    const lines = Array.isArray(logs) ? logs.join('\n') : (explain.detail || '');
    detail.textContent = lines || explain.detail || '';
  }
}

function showRestoreDone(result) {
  stopRestorePoll();
  setRestoreStage('done');
  applyPhaseI18n();
  const access = { ...(state.panelAccess || {}), ...(result || {}) };
  state.panelAccess = access;
  const link = document.getElementById('restorePanelLink');
  const url = resolveLoginUrl(access);
  if (link) {
    link.href = url || '#';
    link.textContent = t('restore.openPanel');
  }
  renderGuideSections(document.getElementById('restoreAccessNotes'), access);
  const tip = document.getElementById('restoreUninstallTip');
  const btn = document.getElementById('btnUninstallRestore');
  const title = document.getElementById('restoreUninstallTitle');
  if (title) title.textContent = t('uninstall.title');
  if (tip) tip.textContent = t('uninstall.tip');
  if (btn) btn.textContent = t('uninstall.button');
}

// Expose for inline handlers / debugging
window.showPhase = showPhase;
window.startWizard = startWizard;
window.startWizardGoal = startWizardGoal;
window.continueAfterPgReady = continueAfterPgReady;
window.backFromRestore = backFromRestore;
window.selectPgDb = selectPgDb;
window.selectPgSsl = selectPgSsl;
window.startPgInstall = startPgInstall;
window.choosePath = choosePath;
window.startRestore = startRestore;
window.resetRestoreForm = resetRestoreForm;
window.setRestoreStage = setRestoreStage;
