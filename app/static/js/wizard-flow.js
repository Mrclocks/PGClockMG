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
  cleanupPlan: null,
  cleanupSelected: null,
});

let _restorePollTimer = null;

function stopRestorePoll() {
  if (_restorePollTimer) {
    clearTimeout(_restorePollTimer);
    _restorePollTimer = null;
  }
}

/** Append-only log update (avoids rewriting huge textContent every poll). */
function appendJobLogs(term, logs, cursor, job) {
  if (!term || !Array.isArray(logs) || !logs.length) return cursor.lastLen;
  const start = Number.isInteger(job?.log_start) ? job.log_start : 0;
  const total = Number.isInteger(job?.log_total) ? job.log_total : start + logs.length;
  if (total <= cursor.lastLen) return cursor.lastLen;
  const skip = Math.max(0, cursor.lastLen - start);
  if (skip >= logs.length) {
    cursor.lastLen = total;
    return cursor.lastLen;
  }
  const chunk = logs.slice(skip).join('\n');
  const prefix = term.textContent ? '\n' : '';
  term.appendChild(document.createTextNode(prefix + chunk));
  cursor.lastLen = total;
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
    flash(t('copyFailed') || 'Copy failed', 'copy-failed');
    await showAppModal({
      title: t('copyManual.title') || t('copy'),
      body: t('copyManual.body') || '',
      copyText: text,
      okText: t('uninstall.ok') || 'OK',
      showCancel: false,
    });
  }
}

function applyUiProgress(fillEl, textEl, pct, key) {
  const next = Math.max(0, Math.min(100, Number(pct) || 0));
  const prev = Number(state[key] || 0);
  const shown = Math.max(prev, next);
  state[key] = shown;
  if (fillEl) fillEl.style.width = `${shown}%`;
  if (textEl) textEl.textContent = `${Math.round(shown)}%`;
  return shown;
}

function resetUiProgress(key) {
  state[key] = 0;
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
  // Prefer server-built URL (already includes UVICORN_ROOT_PATH from .env)
  const preferred = a.login_url || a.panel_url || a.public_url || a.localhost_url || '';
  if (preferred) return preferred;
  const host = (state.pgDomain || a.domain || state.pgIp || a.host || a.ip || '').trim();
  const port = a.port || '8000';
  const root = (a.root_path && a.root_path !== '/' ? a.root_path : '') || '';
  if (host && host !== '127.0.0.1' && host !== 'localhost') {
    const path = `${root}/dashboard/`.replace(/\/{2,}/g, '/');
    const p = path.startsWith('/') ? path : `/${path}`;
    return `https://${host}:${port}${p}`;
  }
  return '';
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

let _appModalResolver = null;

function showAppModal({ title, body, okText, cancelText, danger = false, showCancel = false, copyText = '' }) {
  return new Promise((resolve) => {
    const overlay = document.getElementById('appModal');
    const titleEl = document.getElementById('appModalTitle');
    const bodyEl = document.getElementById('appModalBody');
    const okBtn = document.getElementById('appModalOk');
    const cancelBtn = document.getElementById('appModalCancel');
    const copyWrap = document.getElementById('appModalCopyWrap');
    const copyEl = document.getElementById('appModalCopyText');
    if (!overlay || !okBtn) {
      resolve(true);
      return;
    }
    if (_appModalResolver) {
      _appModalResolver(false);
      _appModalResolver = null;
    }
    _appModalResolver = resolve;
    titleEl.textContent = title || t('uninstall.noticeTitle') || 'Notice';
    bodyEl.textContent = body || '';
    okBtn.textContent = okText || t('uninstall.ok') || 'OK';
    okBtn.className = danger ? 'btn btn-ghost btn-danger' : 'btn btn-primary';
    if (showCancel) {
      cancelBtn.classList.remove('hidden');
      cancelBtn.textContent = cancelText || t('uninstall.cancel') || 'Cancel';
    } else {
      cancelBtn.classList.add('hidden');
    }
    if (copyText && copyWrap && copyEl) {
      copyWrap.classList.remove('hidden');
      copyEl.textContent = copyText;
    } else if (copyWrap) {
      copyWrap.classList.add('hidden');
      if (copyEl) copyEl.textContent = '';
    }
    overlay.classList.remove('hidden');
    okBtn.focus();
  });
}

function closeAppModal(result) {
  const overlay = document.getElementById('appModal');
  if (overlay) overlay.classList.add('hidden');
  if (_appModalResolver) {
    const r = _appModalResolver;
    _appModalResolver = null;
    r(!!result);
  }
}

function bindAppModal() {
  const overlay = document.getElementById('appModal');
  const okBtn = document.getElementById('appModalOk');
  const cancelBtn = document.getElementById('appModalCancel');
  if (!overlay || overlay.dataset.bound) return;
  overlay.dataset.bound = '1';
  okBtn?.addEventListener('click', () => closeAppModal(true));
  cancelBtn?.addEventListener('click', () => closeAppModal(false));
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeAppModal(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay && !overlay.classList.contains('hidden')) {
      closeAppModal(false);
    }
  });
}

async function uninstallWizard(skipConfirm) {
  const errEls = [
    document.getElementById('restoreUninstallErr'),
    document.getElementById('migrateUninstallErr'),
  ];
  errEls.forEach(el => { if (el) { el.textContent = ''; el.classList.add('hidden'); } });

  if (!skipConfirm) {
    const ok = await showAppModal({
      title: t('uninstall.confirmTitle') || t('uninstall.title'),
      body: t('uninstall.confirm'),
      okText: t('uninstall.confirmBtn') || t('uninstall.button'),
      cancelText: t('uninstall.cancel'),
      showCancel: true,
      danger: true,
    });
    if (!ok) return;
  }
  try {
    const res = await fetch('/api/self-uninstall', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (typeof data.detail === 'string' ? data.detail : null)
        || data.message
        || t('uninstall.failed');
      throw new Error(msg);
    }
    if (!skipConfirm) {
      await showAppModal({
        title: t('uninstall.noticeTitle'),
        body: t('uninstall.scheduled'),
        okText: t('uninstall.ok'),
        showCancel: false,
      });
    }
  } catch (e) {
    const msg = e.message || String(e) || t('uninstall.failed');
    errEls.forEach(el => {
      if (!el) return;
      el.textContent = msg;
      el.classList.remove('hidden');
    });
    if (skipConfirm) {
      await showAppModal({
        title: t('uninstall.errorTitle'),
        body: msg,
        okText: t('uninstall.ok'),
        showCancel: false,
        danger: true,
      });
    }
  }
}

window.copyText = copyText;
window.uninstallWizard = uninstallWizard;
window.bindAppModal = bindAppModal;

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
  // Guide tab never "installs" — if user came from guide, stay/return home
  if (goal === 'install') {
    showPhase('pg');
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
  set('welcomeBackupWarn', 'welcome.backupWarning');
  set('welcomeGoalHint', 'welcome.goalHint');
  set('welcomeGoalInstall', 'welcome.goalInstall');
  set('welcomeGoalInstallDesc', 'welcome.goalInstallDesc');
  set('welcomeGoalChangeDb', 'welcome.goalChangeDb');
  set('welcomeGoalChangeDbDesc', 'welcome.goalChangeDbDesc');
  set('welcomeGoalMigrate', 'welcome.goalMigrate');
  set('welcomeGoalMigrateDesc', 'welcome.goalMigrateDesc');
  updateWelcomePgStatus();

  set('pgH2', 'pg.h2');
  set('pgDesc', 'pg.desc');
  set('pgInstalledTitle', 'pg.installedTitle');
  set('pgInstalledDetail', 'pg.installedDetail');
  set('btnPgBack', 'pg.back');
  set('btnPgInstalledBack', 'pg.back');
  set('btnPgOpenPanel', 'pg.openPanel');
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
  set('restoreStreamHint', 'restore.streamHint');
  set('btnStreamListen', 'restore.streamListen');
  const streamSteps = document.getElementById('restoreStreamSteps');
  if (streamSteps) {
    streamSteps.innerHTML = [t('restore.streamStep1'), t('restore.streamStep2'), t('restore.streamStep3')]
      .map((s) => `<li>${escapeHtml(s)}</li>`).join('');
  }
  const streamTag = document.getElementById('restoreStreamStatusTag');
  if (streamTag && !streamTag.dataset.live) {
    streamTag.classList.remove('is-connected', 'is-disconnected');
    streamTag.classList.add('is-unknown');
    streamTag.textContent = t('restore.streamIdle');
  }
  set('btnRestoreConfirm', 'restore.confirm');
  set('btnRestoreBack', 'restore.back');
  set('restoreDoneTitle', 'restore.doneTitle');
  set('restorePanelLabel', 'restore.openPanel');
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
  // Re-render options labels in case language changed while analysis is showing
  if (state.restoreAnalysis) {
    renderRestoreOptions(state.restoreAnalysis);
    renderRestoreDbInfoCard(state.restoreAnalysis);
    renderRestoreCleanup(state.restoreAnalysis);
  }
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

function formatDbLabel(db) {
  const raw = String(db || '').trim().toLowerCase();
  if (!raw || raw === '—' || raw === '-') return '';
  const map = {
    sqlite: 'SQLite',
    mysql: 'MySQL',
    mariadb: 'MariaDB',
    postgresql: 'PostgreSQL',
    postgres: 'PostgreSQL',
    timescaledb: 'TimescaleDB',
  };
  return map[raw] || db;
}

function updateWelcomePgStatus() {
  const el = document.getElementById('welcomePgStatus');
  if (!el) return;
  const sys = state.systemCheck || {};
  const installed = !!(sys.pasarguard || state.panelAccess?.installed);
  el.classList.remove('hidden', 'warning-card', 'success-card');
  if (installed) {
    const db = formatDbLabel(sys.pasarguard_db || state.panelAccess?.db_type) || '—';
    el.classList.add('success-card');
    el.textContent = String(t('welcome.pgInstalled') || '').replace('{db}', db);
  } else {
    el.classList.add('warning-card');
    el.textContent = t('welcome.pgMissing');
  }
}

function renderInstalledSpecs() {
  const el = document.getElementById('pgInstalledSpecs');
  if (!el) return;
  const access = state.panelAccess || {};
  const sys = state.systemCheck || {};
  const db = sys.pasarguard_db || access.db_type || '—';
  const port = access.port || sys.pasarguard_env?.panel_port || '8000';
  const ssl = access.ssl === true ? t('pg.specSslYes') : (access.ssl === false ? t('pg.specSslNo') : '—');
  const url = access.login_url || access.dashboard_url || '';
  const rows = [
    [t('pg.specPath'), '/opt/pasarguard', 'path'],
    [t('pg.specDb'), db, 'text'],
    [t('pg.specPort'), String(port), 'text'],
    [t('pg.specSsl'), ssl, 'text'],
    [t('pg.specEnv'), '/opt/pasarguard/.env', 'path'],
    [t('pg.specUrl'), url || '—', url ? 'url' : 'text'],
  ];
  el.innerHTML = rows.map(([label, value, kind]) => {
    let valHtml;
    if (kind === 'url') {
      valHtml = `<a class="specs-value" href="${escapeHtml(value)}" target="_blank" rel="noopener" title="${escapeHtml(value)}">${escapeHtml(value)}</a>`;
    } else if (kind === 'path') {
      valHtml = `<code class="specs-value" title="${escapeHtml(value)}">${escapeHtml(value)}</code>`;
    } else {
      valHtml = `<span class="specs-value" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`;
    }
    return `<div class="specs-item"><span class="specs-label">${escapeHtml(label)}</span>${valHtml}</div>`;
  }).join('');
}

function openInstalledPanel() {
  const url = resolveLoginUrl(state.panelAccess) || state.panelAccess?.login_url;
  if (url) window.open(url, '_blank', 'noopener');
}
window.openInstalledPanel = openInstalledPanel;

async function renderPgSetup() {
  await loadSystemCheck();
  await refreshPanelAccess();
  const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
  const installedCard = document.getElementById('pgInstalledCard');
  const guide = document.getElementById('pgInstallGuide');

  // If installed: ONLY specs. If not: ONLY manual guide. Never auto-install.
  if (installed) {
    guide?.classList.add('hidden');
    installedCard?.classList.remove('hidden');
    document.getElementById('pgInstalledActions')?.classList.remove('hidden');
    const db = formatDbLabel(state.systemCheck?.pasarguard_db || state.panelAccess?.db_type);
    const title = document.getElementById('pgInstalledTitle');
    if (title) {
      title.textContent = db
        ? String(t('pg.installedTitleDb') || t('pg.installedTitle')).replace('{db}', db)
        : t('pg.installedTitle');
    }
    const detail = document.getElementById('pgInstalledDetail');
    if (detail) detail.textContent = t('pg.installedDetail');
    const openBtn = document.getElementById('btnPgOpenPanel');
    if (openBtn) openBtn.textContent = t('pg.openPanel');
    const backBtn = document.getElementById('btnPgInstalledBack');
    if (backBtn) backBtn.textContent = t('pg.back');
    renderInstalledSpecs();
    // Title/desc for status mode
    const h2 = document.getElementById('pgH2');
    const desc = document.getElementById('pgDesc');
    if (h2) h2.textContent = t('pg.h2Installed');
    if (desc) desc.textContent = t('pg.descInstalled');
  } else {
    installedCard?.classList.add('hidden');
    document.getElementById('pgInstalledActions')?.classList.add('hidden');
    guide?.classList.remove('hidden');
    renderInstallCmdList();
    renderTutorialSteps();
    const h2 = document.getElementById('pgH2');
    const desc = document.getElementById('pgDesc');
    if (h2) h2.textContent = t('pg.h2');
    if (desc) desc.textContent = t('pg.desc');
  }
  updateWelcomePgStatus();
}

async function recheckAfterManualInstall() {
  const btn = document.getElementById('btnPgRecheck');
  if (btn) btn.disabled = true;
  try {
    await loadSystemCheck();
    await refreshPanelAccess();
    await renderPgSetup();
    const installed = !!(state.systemCheck?.pasarguard || state.panelAccess?.installed);
    // After user installs manually, resume pending restore/migrate
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
  const btn = document.getElementById('btnRestoreConfirm');
  const progressIds = {
    zone: 'restoreUploadZone',
    panel: 'restoreUploadProgress',
    fill: 'restoreUploadProgressFill',
    pct: 'restoreUploadProgressPct',
    msg: 'restoreUploadProgressMsg',
    status: 'restoreUploadStatus',
    replaceBtn: 'restoreUploadReplaceBtn',
    onReplace: () => {
      setUploadProgressUi(progressIds, { phase: 'idle' });
      state.restoreUploadId = null;
      state.restoreAnalysis = null;
      if (btn) btn.disabled = true;
      document.getElementById('restoreFileInput')?.click();
    },
  };

  if (btn) btn.disabled = true;
  state.restoreAnalysis = null;

  setUploadProgressUi(progressIds, {
    phase: 'uploading',
    pct: 0,
    message: `${t('uploadProgress')} (${file.name})`,
  });

  const fd = new FormData();
  fd.append('file', file);
  if (typeof largeUploadOverrideEnabled === 'function' && largeUploadOverrideEnabled()) {
    fd.append('allow_large_upload', '1');
  }
  try {
    const data = await uploadFormWithProgress('/api/upload', fd, (pct) => {
      setUploadProgressUi(progressIds, {
        phase: 'uploading',
        pct: pct == null ? 0 : pct,
        message: `${t('uploadProgress')} (${file.name})`,
      });
    });
    if (!data?.upload_id) throw new Error('upload failed');
    state.restoreUploadId = data.upload_id;

    setUploadProgressUi(progressIds, {
      phase: 'uploading',
      pct: 100,
      message: t('restore.analyzing'),
    });

    const ares = await fetch(`/api/pasarguard/restore/analyze/${data.upload_id}`);
    const analysis = await ares.json();
    if (!ares.ok) throw new Error(analysis.detail || 'analyze failed');
    state.restoreAnalysis = analysis;
    await loadCleanupPlan(data.upload_id);
    renderRestoreAnalysis(analysis);
    if (btn) btn.disabled = !analysis.ok;

    document.getElementById('restoreUploadZone')?.classList.add('hidden');
    document.getElementById('restoreUploadProgress')?.classList.add('hidden');
    applyUploadSuccessStatus(document.getElementById('restoreUploadStatus'), {
      ok: !!analysis.ok,
      message: t('uploadSuccess'),
      fileName: file.name,
      replaceId: 'restoreUploadReplaceBtn',
      onReplace: progressIds.onReplace,
    });
  } catch (e) {
    setUploadProgressUi(progressIds, { phase: 'error', message: e.message });
    if (btn) btn.disabled = true;
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

const DB_DISPLAY_NAMES = {
  timescaledb: 'TimescaleDB',
  postgresql: 'PostgreSQL',
  mysql: 'MySQL',
  mariadb: 'MariaDB',
  sqlite: 'SQLite',
};

function dbLabel(key) {
  return DB_DISPLAY_NAMES[key] || (key ? key.charAt(0).toUpperCase() + key.slice(1) : '—');
}

function renderRestoreDbInfoCard(a) {
  const card = document.getElementById('restoreDbInfoCard');
  if (!card || !a) return;
  if (!a.backup_db && !a.installed_db) {
    card.classList.add('hidden');
    return;
  }
  const lang = state.lang || 'fa';
  const s = (I18N[lang] || I18N.fa).restore.dbInfo;

  const bkDb = dbLabel(a.backup_db);
  const instDb = dbLabel(a.installed_db);

  let matchClass = 'db-match-ok';
  let matchText = '';
  if (a.convert_blocked) {
    matchClass = 'db-match-blocked';
    matchText = s.matchBlocked;
  } else if (a.db_match) {
    matchClass = 'db-match-ok';
    matchText = s.matchYes;
  } else if (a.soft_match) {
    matchClass = 'db-match-soft';
    matchText = s.matchSoft;
  } else if (a.experimental_db_change) {
    matchClass = 'db-match-convert';
    matchText = s.matchConvert;
  } else if (a.ok) {
    matchClass = 'db-match-convert';
    matchText = s.matchConvert;
  } else {
    matchClass = 'db-match-blocked';
    matchText = s.matchBlocked;
  }

  let tsHtml = '';
  if (a.timescaledb_versions && a.timescaledb_versions.length > 0) {
    tsHtml = `<div class="db-info-row">
      <span class="db-info-label">${s.tsVersions}</span>
      <span class="db-info-value">${a.timescaledb_versions.join(', ')}</span>
    </div>`;
  }

  let layoutHtml = '';
  if (a.layout && a.layout !== 'none') {
    layoutHtml = `<div class="db-info-row">
      <span class="db-info-label">${s.layout}</span>
      <span class="db-info-value">${a.layout}</span>
    </div>`;
  }

  card.innerHTML = `
    <div class="db-info-body">
      <div class="db-info-engines">
        <div class="db-info-engine">
          <span class="db-info-engine-label">${s.backupDb}</span>
          <span class="db-badge db-badge-backup">${bkDb}</span>
        </div>
        <div class="db-info-arrow" aria-hidden="true">
          <svg viewBox="0 0 20 20" fill="none" width="18" height="18"><path d="M4 10h12M12 6l4 4-4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div class="db-info-engine">
          <span class="db-info-engine-label">${s.installedDb}</span>
          <span class="db-badge db-badge-installed">${instDb}</span>
        </div>
      </div>
      <div class="db-info-match ${matchClass}">${matchText}</div>
      ${tsHtml}${layoutHtml}
    </div>
  `;
  card.classList.remove('hidden');
}

function renderRestoreOptions(a) {
  const opts = document.getElementById('restoreOptions');
  const lbl = document.getElementById('chkDisableNodesLabel');
  const hint = document.getElementById('chkDisableNodesHint');
  const chk = document.getElementById('chkDisableNodes');
  if (!opts) return;
  if (!a || !a.ok || a.convert_blocked) {
    opts.classList.add('hidden');
    return;
  }
  const lang = state.lang || 'fa';
  const s = (I18N[lang] || I18N.fa).restore;
  if (lbl) lbl.textContent = s.disableNodes;
  if (hint) hint.textContent = s.disableNodesHint;
  if (chk) chk.checked = false;
  opts.classList.remove('hidden');
}

function formatBytesShort(n) {
  const bytes = Number(n) || 0;
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function formatCountShort(n) {
  const locales = { fa: 'fa-IR', ru: 'ru-RU', en: 'en-US' };
  return (Number(n) || 0).toLocaleString(locales[state.lang] || 'en-US');
}

// Cleanup is a bonus: any failure here must leave the restore exactly as it was
// without it, so every path ends with the plan simply not being offered.
async function loadCleanupPlan(uploadId) {
  state.cleanupPlan = null;
  state.cleanupSelected = null;
  try {
    const res = await fetch(`/api/pasarguard/cleanup/analyze/${uploadId}`);
    if (!res.ok) return;
    const plan = await res.json();
    if (!plan || plan.available !== true) return;
    if (!(plan.removable_rows > 0)) return;
    state.cleanupPlan = plan;
    state.cleanupSelected = new Set(plan.default_rule_ids || []);
  } catch (e) {
    state.cleanupPlan = null;
  }
}

function renderRestoreCleanup(a) {
  const card = document.getElementById('restoreCleanup');
  if (!card) return;
  const plan = state.cleanupPlan;
  const offer = !!plan && !!a && !!a.ok && !a.convert_blocked;
  if (!offer) {
    card.classList.add('hidden');
    return;
  }

  const lang = state.lang || 'fa';
  const s = (I18N[lang] || I18N.fa).restore.cleanup;
  const selected = state.cleanupSelected || new Set();

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  setText('restoreCleanupTitle', s.title);
  setText('restoreCleanupDesc', s.desc);
  setText('restoreCleanupNote', s.note);
  setText('restoreCleanupBadge', fmtMsg(s.badge, { size: formatBytesShort(plan.removable_bytes) }));

  const rows = (plan.rules || [])
    .filter((r) => r.rows > 0)
    .map((r) => {
      const id = `cleanupRule_${r.id}`;
      const checked = selected.has(r.id) ? ' checked' : '';
      const size = r.bytes > 0 ? ` · ${formatBytesShort(r.bytes)}` : '';
      const amount = fmtMsg(s.rows, { rows: formatCountShort(r.rows) }) + size;
      return `
        <div class="toggle-row">
          <label class="ios-toggle" for="${id}">
            <input type="checkbox" id="${id}" role="switch" data-cleanup-rule="${escapeHtml(r.id)}"${checked}>
            <span class="ios-toggle-track"><span class="ios-toggle-thumb"></span></span>
          </label>
          <div class="toggle-text">
            <span class="toggle-label">${escapeHtml(tr(r.label, lang))} <span class="cleanup-amount">${escapeHtml(amount)}</span></span>
            <span class="toggle-hint">${escapeHtml(tr(r.description, lang))}</span>
          </div>
        </div>`;
    })
    .join('');

  const list = document.getElementById('restoreCleanupRules');
  if (list) {
    list.innerHTML = rows;
    list.querySelectorAll('input[data-cleanup-rule]').forEach((input) => {
      input.addEventListener('change', () => {
        const rid = input.getAttribute('data-cleanup-rule');
        if (!state.cleanupSelected) state.cleanupSelected = new Set();
        if (input.checked) state.cleanupSelected.add(rid);
        else state.cleanupSelected.delete(rid);
      });
    });
  }
  card.classList.remove('hidden');
}

function renderRestoreAnalysis(a) {
  const card = document.getElementById('restoreAnalysis');
  const warn = document.getElementById('restoreWarnings');
  const note = document.getElementById('restoreConvertNote');
  if (card) {
    card.classList.add('hidden');
    card.innerHTML = '';
  }
  if (warn) {
    warn.classList.add('hidden');
    warn.innerHTML = '';
  }
  if (note) note.classList.add('hidden');

  renderRestoreDbInfoCard(a);
  renderRestoreOptions(a);
  renderRestoreCleanup(a);

  const block = document.getElementById('restoreBlock');
  const lang = state.lang || 'fa';
  const blocking = !(a && a.ok) || !!a?.convert_blocked;
  if (block) {
    if (blocking) {
      const msgs = (a?.warnings || [])
        .map((w) => tr(w, lang))
        .filter(Boolean);
      block.textContent = msgs[0] || t('restore.confirmNeeded');
      block.classList.remove('hidden');
    } else {
      block.textContent = '';
      block.classList.add('hidden');
    }
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

// Returns the upload_id the restore should use. Falls back to the uploaded one
// whenever slimming is not offered, not selected, or does not succeed.
async function applyCleanupBeforeRestore(term) {
  const original = state.restoreUploadId;
  const selected = Array.from(state.cleanupSelected || []);
  if (!state.cleanupPlan || !selected.length) return original;

  const lang = state.lang || 'fa';
  const s = (I18N[lang] || I18N.fa).restore.cleanup;
  const note = (msg) => {
    if (term) term.textContent += `${msg}\n`;
  };

  const status = document.getElementById('restoreStatusMsg');
  if (status) status.textContent = s.working;
  note(s.working);

  try {
    const res = await fetch('/api/pasarguard/cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ upload_id: original, rule_ids: selected }),
    });
    const data = await res.json();
    if (!res.ok || !data?.applied || !data.upload_id) {
      note(s.skipped);
      return original;
    }
    note(fmtMsg(s.applied, {
      rows: formatCountShort(data.removed_rows),
      size: formatBytesShort(Math.max((data.size_before || 0) - (data.size_after || 0), 0)),
    }));
    return data.upload_id;
  } catch (e) {
    note(s.skipped);
    return original;
  } finally {
    if (status) status.textContent = t('restore.restoring');
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

  stopRestorePoll();
  setRestoreStage('running');
  applyPhaseI18n();
  resetUiProgress('_restoreUiProgress');
  const fill = document.getElementById('restoreProgressFill');
  const text = document.getElementById('restoreProgressText');
  const status = document.getElementById('restoreStatusMsg');
  const term = document.getElementById('restoreLogTerminal');
  if (fill) fill.style.width = '0%';
  if (text) text.textContent = '0%';
  if (status) status.textContent = t('restore.restoring');
  if (term) term.textContent = '';

  const disableNodes = document.getElementById('chkDisableNodes')?.checked || false;
  const uploadId = await applyCleanupBeforeRestore(term);

  try {
    const res = await fetch('/api/pasarguard/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upload_id: uploadId,
        confirmed: true,
        force: false,
        // Destination is always the installed PasarGuard DB
        target_db: state.restoreAnalysis.installed_db || undefined,
        accept_experimental: true,
        disable_nodes_after_restore: disableNodes,
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
      const res = await fetch(`/api/pasarguard/restore/${jobId}?since=${cursor.lastLen}`);
      const job = await res.json();
      applyUiProgress(fill, text, job.progress || 0, '_restoreUiProgress');
      if (status) status.textContent = job.message || t('restore.restoring');
      appendJobLogs(term, job.logs, cursor, job);

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
    const label = document.getElementById('restorePanelLabel');
    if (label) label.textContent = t('restore.openPanel');
  }
  const urlLine = document.getElementById('restorePanelUrl');
  if (urlLine) {
    if (url) {
      urlLine.textContent = url;
      urlLine.classList.remove('hidden');
    } else {
      urlLine.textContent = '';
      urlLine.classList.add('hidden');
    }
  }
  const msg = document.getElementById('restoreDoneMsg');
  if (msg) {
    const convert = access.auto_db_convert
      ? ` (${access.backup_db || '?'} → ${access.final_db || '?'})`
      : '';
    msg.textContent = `${t('restore.doneTitle') || ''}${convert}`.trim();
  }
  const nodesNote = document.getElementById('restoreNodesDisabledNote');
  if (nodesNote) {
    if (access.nodes_disabled) {
      nodesNote.textContent = t('restore.nodesDisabledNote');
      nodesNote.classList.remove('hidden');
    } else {
      nodesNote.classList.add('hidden');
    }
  }
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
window.applyUiProgress = applyUiProgress;
window.resetUiProgress = resetUiProgress;
window.resetRestoreForm = resetRestoreForm;
window.setRestoreStage = setRestoreStage;
window.startStreamListen = startStreamListen;

let _streamPollTimer = null;

async function startStreamListen() {
  const status = document.getElementById('restoreStreamStatus');
  const tokenBox = document.getElementById('restoreStreamTokenBox');
  const tokenEl = document.getElementById('restoreStreamToken');
  const btn = document.getElementById('btnStreamListen');
  const prog = document.getElementById('restoreStreamProgress');
  const pctEl = document.getElementById('restoreStreamProgressPct');
  const fill = document.getElementById('restoreStreamProgressFill');
  if (_streamPollTimer) {
    clearTimeout(_streamPollTimer);
    _streamPollTimer = null;
  }
  const setTag = (state) => {
    const el = document.getElementById('restoreStreamStatusTag');
    if (!el) return;
    el.classList.remove('is-connected', 'is-disconnected', 'is-unknown');
    if (state === 'listening') {
      el.classList.add('is-unknown');
      el.textContent = t('restore.streamListening');
    } else if (state === 'receiving') {
      el.classList.add('is-connected');
      el.textContent = t('restore.streamConnected');
    } else if (state === 'ready') {
      el.classList.add('is-connected');
      el.textContent = t('restore.streamReady');
    } else if (state === 'error') {
      el.classList.add('is-disconnected');
      el.textContent = t('restore.streamError');
    } else {
      el.classList.add('is-unknown');
      el.textContent = t('restore.streamIdle');
    }
  };
  const setProgress = (pct, label) => {
    if (prog) prog.classList.remove('hidden');
    if (status) status.textContent = label || '';
    const width = Math.max(0, Math.min(100, Number(pct) || 0));
    if (pctEl) pctEl.textContent = `${width}%`;
    if (fill) fill.style.width = `${width}%`;
  };
  const human = (n) => {
    const v = Number(n) || 0;
    if (v < 1024) return `${v} B`;
    if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
    if (v < 1024 * 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MB`;
    return `${(v / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  };
  try {
    if (btn) btn.disabled = true;
    const res = await fetch('/api/stream/listen', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'listen failed');
    tokenEl.textContent = data.token;
    tokenBox.classList.remove('hidden');
    setTag('listening');
    setProgress(0, t('restore.streamWaiting'));
    const poll = async () => {
      try {
        const sres = await fetch(`/api/stream/status/${encodeURIComponent(data.token)}`);
        const st = await sres.json();
        if (!sres.ok) throw new Error(st.detail || 'status failed');
        const got = Number(st.bytes_received) || 0;
        const total = Number(st.expected_size) || 0;
        if (st.status === 'receiving') {
          setTag('receiving');
          const pct = total > 0 ? Math.round((got / total) * 100) : Math.min(95, Math.max(5, Math.round(got / (1024 * 1024))));
          const meta = total > 0 ? `${human(got)} / ${human(total)}` : human(got);
          setProgress(pct, `${t('restore.streamReceiving')} ${meta}`);
        }
        if (st.status === 'ready' && st.upload_id) {
          setTag('ready');
          setProgress(100, t('restore.streamReady'));
          await applyStreamedBackup(st.upload_id, st.filename || 'streamed-backup.zip');
          if (btn) btn.disabled = false;
          return;
        }
        if (st.status === 'error') {
          setTag('error');
          setProgress(100, `${t('restore.streamError')}: ${st.error || ''}`);
          if (btn) btn.disabled = false;
          return;
        }
        _streamPollTimer = setTimeout(poll, 800);
      } catch (e) {
        setTag('error');
        setProgress(100, `${t('restore.streamError')}: ${e.message}`);
        if (btn) btn.disabled = false;
      }
    };
    poll();
  } catch (e) {
    setTag('error');
    if (prog) prog.classList.remove('hidden');
    if (status) status.textContent = `${t('restore.streamError')}: ${e.message}`;
    if (btn) btn.disabled = false;
  }
}

async function applyStreamedBackup(uploadId, fileName) {
  const btn = document.getElementById('btnRestoreConfirm');
  state.restoreUploadId = uploadId;
  const ares = await fetch(`/api/pasarguard/restore/analyze/${uploadId}`);
  const analysis = await ares.json();
  if (!ares.ok) throw new Error(analysis.detail || 'analyze failed');
  state.restoreAnalysis = analysis;
  await loadCleanupPlan(uploadId);
  renderRestoreAnalysis(analysis);
  if (btn) btn.disabled = !analysis.ok;
  document.getElementById('restoreUploadZone')?.classList.add('hidden');
  applyUploadSuccessStatus(document.getElementById('restoreUploadStatus'), {
    ok: !!analysis.ok,
    message: t('uploadSuccess'),
    fileName,
    replaceId: 'restoreUploadReplaceBtn',
    onReplace: () => {
      state.restoreUploadId = null;
      state.restoreAnalysis = null;
      if (btn) btn.disabled = true;
      document.getElementById('restoreUploadZone')?.classList.remove('hidden');
      document.getElementById('restoreStreamTokenBox')?.classList.add('hidden');
    },
  });
}
