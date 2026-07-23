/* Pre-migration wizard phases: welcome → install guide | change_db | migrate */

Object.assign(state, {
  phase: 'welcome', // welcome | pg | choose | restore | migrate
  wizardGoal: null, // install | change_db | migrate
  panelAccess: null,
  installGuide: null,
  restoreUploadId: null,
  restoreAnalysis: null,
  restoreStage: 'form', // form | running | error | done
  pendingLoginUrl: null,
});

let _restorePollTimer = null;

function stopRestorePoll() {
  if (_restorePollTimer) {
    clearTimeout(_restorePollTimer);
    _restorePollTimer = null;
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

function writeClipboard(text) {
  // navigator.clipboard is only available in secure contexts (https or localhost).
  // The wizard is usually served over http://SERVER_IP:7000, so fall back to
  // the legacy execCommand('copy') via a temporary textarea.
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ta.setSelectionRange(0, text.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error('execCommand copy failed'));
    } catch (e) {
      reject(e);
    }
  });
}

async function copyText(codeOrId, raw) {
  const el = typeof codeOrId === 'string' ? document.getElementById(codeOrId) : null;
  const text = (raw != null ? String(raw) : (el?.textContent || codeOrId || '')).trim();
  // Capture the button synchronously — `event` is not reliable after an await.
  const btn = (typeof event !== 'undefined' && (event?.currentTarget || event?.target)) || null;
  const flash = (label, cls) => {
    if (!btn || !btn.classList) return;
    const prev = btn.dataset.origLabel || btn.textContent;
    btn.dataset.origLabel = prev;
    btn.textContent = label;
    if (cls) btn.classList.add(cls);
    setTimeout(() => {
      btn.textContent = btn.dataset.origLabel || prev;
      if (cls) btn.classList.remove(cls);
    }, 1600);
  };
  try {
    await writeClipboard(text);
    flash(t('copied'), 'copied');
  } catch (e) {
    // Last resort: show the text so the user can copy manually.
    flash(t('copyFailed') || 'Copy failed', 'copy-failed');
    window.prompt(t('copy') || 'Copy', text);
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

  // Install tab = official command guide (never auto-installs)
  if (goal === 'install') {
    showPhase('pg');
    return;
  }

  // Restore / migrate require PasarGuard first
  if (!installed) {
    openNeedPgModal();
    return;
  }
  await continueAfterPgReady();
}

/** Legacy entry — keep for any leftover callers */
function startWizard() {
  startWizardGoal(state.wizardGoal || 'install');
}

/** After PG is confirmed installed, jump to chosen goal. */
async function continueAfterPgReady() {
  const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
  if (!installed) {
    openNeedPgModal();
    return;
  }
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
  showPhase('welcome');
}

function cancelMigrationRun() {
  if (typeof stopMigrationPoll === 'function') stopMigrationPoll();
  else if (window._migrationPollTimer) {
    clearTimeout(window._migrationPollTimer);
    window._migrationPollTimer = null;
  }
  goStep(4);
}

window.cancelMigrationRun = cancelMigrationRun;

function openNeedPgModal() {
  applyPhaseI18n();
  document.getElementById('needPgModal')?.classList.remove('hidden');
}

function closeNeedPgModal() {
  document.getElementById('needPgModal')?.classList.add('hidden');
}

function goToInstallGuide() {
  closeNeedPgModal();
  if (!state.wizardGoal) state.wizardGoal = 'change_db';
  showPhase('pg');
}

window.openNeedPgModal = openNeedPgModal;
window.closeNeedPgModal = closeNeedPgModal;
window.goToInstallGuide = goToInstallGuide;

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
  set('pgGuideIntro', 'pg.guideIntro');
  set('pgCmdsTitle', 'pg.cmdsTitle');
  set('pgCmdsHint', 'pg.cmdsHint');
  set('pgTutorialTitle', 'pg.tutorialTitle');
  set('lblOwnerKey', 'pg.ownerKeyLabel');
  set('pgOwnerKeyHint', 'pg.ownerKeyHint');
  set('lblSshTunnel', 'pg.sshTunnelLabel');
  set('pgSshTunnelHint', 'pg.sshTunnelHint');
  set('pgDocsLink', 'pg.docsLink');
  set('pgGithubLink', 'pg.githubLink');
  set('btnPgRecheck', 'pg.recheck');
  set('btnPgContinueFromGuide', 'pg.continue');
  set('btnCopyOwnerKey', 'copy');
  set('btnCopySshTunnel', 'copy');

  set('needPgModalTitle', 'needPg.title');
  set('needPgModalDesc', 'needPg.desc');
  set('btnNeedPgCancel', 'needPg.cancel');
  set('btnNeedPgGoInstall', 'needPg.goInstall');

  set('installPgMissingTitle', 'step3.pgMissing');
  set('installPgMissingDesc', 'step3.pgMissingDesc');
  const goInstallBtn = document.getElementById('btnGoInstallTab');
  if (goInstallBtn) goInstallBtn.textContent = t('needPg.goInstall');

  renderInstallCmdList();
  renderTutorialSteps();
  applyChooseI18n();

  set('restoreH2', 'restore.h2ChangeDb');
  set('restoreDesc', 'restore.descChangeDb');
  set('restoreDbTipText', 'restore.tip');
  set('restoreDragText', 'restore.drag');
  set('restoreSelectText', 'restore.select');
  set('btnRestoreConfirm', 'restore.confirm');
  set('btnRestoreBack', 'restore.back');
  set('btnRestoreDoneBack', 'restore.back');
  set('restoreDoneTitle', 'restore.doneTitle');
  set('restorePanelLink', 'restore.openPanel');
  set('restoreRunningTitle', 'restore.runningTitle');
  set('restoreRunningDesc', 'restore.runningDesc');
  set('restoreErrorTitle', 'restore.errorTitle');
  set('restoreErrorDetailToggle', 'restore.errorDetail');
  set('btnRestoreErrorBack', 'restore.back');
  set('btnRestoreRetry', 'restore.retry');
  set('btnRestoreRunningBack', 'restore.cancel');
  set('btnStep5Back', 'step4.back');
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

function defaultInstallGuide() {
  const script = 'https://github.com/PasarGuard/scripts/raw/main/pasarguard.sh';
  const mk = (db) => (
    db === 'sqlite'
      ? `curl -fsSL ${script} -o /tmp/pg.sh \\\n  && sudo bash /tmp/pg.sh install`
      : `curl -fsSL ${script} -o /tmp/pg.sh \\\n  && sudo bash /tmp/pg.sh install --database ${db}`
  );
  return {
    docs_url: 'https://docs.pasarguard.org/en/panel/installation/',
    github_url: 'https://github.com/PasarGuard/panel',
    owner_temp_key_cmd: 'pasarguard cli generate-temp-key',
    ssh_tunnel_cmd: 'ssh -L 8000:localhost:8000 user@serverip',
    commands: {
      timescaledb: { label: { en: 'TimescaleDB (Recommended)', fa: 'TimescaleDB (پیشنهادی)', ru: 'TimescaleDB' }, desc: { en: '', fa: '', ru: '' }, cmd: mk('timescaledb') },
      postgresql: { label: { en: 'PostgreSQL', fa: 'PostgreSQL', ru: 'PostgreSQL' }, desc: { en: '', fa: '', ru: '' }, cmd: mk('postgresql') },
      mysql: { label: { en: 'MySQL', fa: 'MySQL', ru: 'MySQL' }, desc: { en: '', fa: '', ru: '' }, cmd: mk('mysql') },
      mariadb: { label: { en: 'MariaDB', fa: 'MariaDB', ru: 'MariaDB' }, desc: { en: '', fa: '', ru: '' }, cmd: mk('mariadb') },
      sqlite: { label: { en: 'SQLite', fa: 'SQLite', ru: 'SQLite' }, desc: { en: '', fa: '', ru: '' }, cmd: mk('sqlite') },
    },
  };
}

function renderInstallCmdList() {
  const list = document.getElementById('pgInstallCmdList');
  if (!list) return;
  const guide = state.installGuide || defaultInstallGuide();
  const order = ['timescaledb', 'postgresql', 'mysql', 'mariadb', 'sqlite'];
  const lang = state.lang || 'fa';
  list.innerHTML = order.map((id) => {
    const item = guide.commands?.[id];
    if (!item) return '';
    const label = (item.label && (item.label[lang] || item.label.fa || item.label.en)) || id;
    const desc = (item.desc && (item.desc[lang] || item.desc.fa || item.desc.en)) || '';
    const cmd = item.cmd || '';
    const codeId = `pgInstallCmd_${id}`;
    const rec = id === 'timescaledb' ? `<span class="db-badge">${escapeHtml(t('dbRecommended'))}</span>` : '';
    return `<div class="install-cmd-card">
      <div class="install-cmd-card-head">
        <strong>${escapeHtml(label)}</strong>${rec}
      </div>
      ${desc ? `<p class="desc-sm">${escapeHtml(desc)}</p>` : ''}
      <div class="install-cmd-row">
        <div class="install-cmd-box"><code id="${codeId}">${escapeHtml(cmd)}</code></div>
        <button type="button" class="btn btn-copy" data-copy-id="${codeId}">${escapeHtml(t('copy'))}</button>
      </div>
    </div>`;
  }).join('');

  list.querySelectorAll('[data-copy-id]').forEach((btn) => {
    btn.addEventListener('click', () => copyText(btn.getAttribute('data-copy-id')));
  });

  const owner = document.getElementById('pgOwnerKeyCmd');
  if (owner) owner.textContent = guide.owner_temp_key_cmd || 'pasarguard cli generate-temp-key';
  const ssh = document.getElementById('pgSshTunnelCmd');
  if (ssh) ssh.textContent = guide.ssh_tunnel_cmd || 'ssh -L 8000:localhost:8000 user@serverip';
  const docs = document.getElementById('pgDocsLink');
  if (docs && guide.docs_url) docs.href = guide.docs_url;
  const gh = document.getElementById('pgGithubLink');
  if (gh && guide.github_url) gh.href = guide.github_url;
}

function renderTutorialSteps() {
  const ol = document.getElementById('pgTutorialSteps');
  if (!ol) return;
  const steps = t('pg.tutorialSteps');
  const items = Array.isArray(steps) ? steps : [];
  ol.innerHTML = items.map((s) => `<li>${escapeHtml(s)}</li>`).join('');
}

async function renderPgSetup() {
  await loadSystemCheck();
  await refreshPanelAccess();
  const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
  const installedCard = document.getElementById('pgInstalledCard');
  const guide = document.getElementById('pgInstallGuide');
  const continueFromGuide = document.getElementById('btnPgContinueFromGuide');

  guide?.classList.remove('hidden');
  renderInstallCmdList();
  renderTutorialSteps();

  if (installed) {
    installedCard?.classList.remove('hidden');
    const detail = document.getElementById('pgInstalledDetail');
    if (detail) {
      const db = state.systemCheck?.pasarguard_db || state.panelAccess?.db_type || '';
      detail.textContent = `${t('pg.installedDetail')}${db ? ` (${db})` : ''}`;
    }
    continueFromGuide?.classList.remove('hidden');
  } else {
    installedCard?.classList.add('hidden');
    continueFromGuide?.classList.add('hidden');
  }
}

async function recheckAfterManualInstall() {
  const btn = document.getElementById('btnPgRecheck');
  if (btn) btn.disabled = true;
  try {
    await loadSystemCheck();
    await refreshPanelAccess();
    await renderPgSetup();
    const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
    if (installed && state.wizardGoal && state.wizardGoal !== 'install') {
      await continueAfterPgReady();
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

window.recheckAfterManualInstall = recheckAfterManualInstall;

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
    const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
    if (!installed) {
      openNeedPgModal();
      return;
    }
    showPhase('restore');
    return;
  }
  if (path === 'migrate') {
    state.wizardGoal = 'migrate';
    const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
    if (!installed) {
      openNeedPgModal();
      return;
    }
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
    const show = needsConvert && !a.convert_blocked;
    note.classList.toggle('hidden', !show);
    const noteText = document.getElementById('restoreConvertNoteText');
    if (noteText) noteText.textContent = t('restore.autoConvertNote');
  }

  updateRestoreConfirmEnabled();
}

function updateRestoreConfirmEnabled() {
  const btn = document.getElementById('btnRestoreConfirm');
  const a = state.restoreAnalysis;
  if (!btn || !a) return;
  // Blocked conversions (e.g. mysql → sqlite) set ok=false
  btn.disabled = !a.ok || !!a.convert_blocked;
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
  const msg = document.getElementById('restoreDoneMsg');
  if (msg) {
    const counts = access.verified_counts || access.copy_stats || {};
    const parts = ['users', 'hosts', 'groups', 'nodes', 'inbounds', 'admins']
      .filter(k => counts[k] != null)
      .map(k => `${k}=${counts[k]}`);
    const convert = access.auto_db_convert
      ? ` (${access.backup_db || '?'} → ${access.final_db || '?'})`
      : '';
    msg.textContent = parts.length
      ? `${t('restore.verifiedCounts') || 'Verified'}: ${parts.join(', ')}${convert}`
      : (t('restore.doneTitle') || '');
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
window.choosePath = choosePath;
window.startRestore = startRestore;
window.resetRestoreForm = resetRestoreForm;
window.setRestoreStage = setRestoreStage;
