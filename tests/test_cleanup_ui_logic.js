/* Cleanup UI decision logic, mirroring wizard-flow.js.
 *
 * The point of these cases is the fail-open contract: whatever the cleanup
 * endpoints do, the restore must still run against a valid upload_id.
 *
 * Run: node tests/test_cleanup_ui_logic.js
 */

const assert = require('assert');

// ── mirrors loadCleanupPlan() ────────────────────────────────────────────────
function shouldOfferCleanup(plan) {
  if (!plan || plan.available !== true) return false;
  if (!(plan.removable_rows > 0)) return false;
  return true;
}

// ── mirrors renderRestoreCleanup() visibility rule ───────────────────────────
function cleanupCardVisible(plan, analysis) {
  return shouldOfferCleanup(plan) && !!analysis && !!analysis.ok && !analysis.convert_blocked;
}

// ── mirrors applyCleanupBeforeRestore() ──────────────────────────────────────
async function resolveRestoreUploadId(state, postCleanup) {
  const original = state.restoreUploadId;
  const selected = Array.from(state.cleanupSelected || []);
  if (!state.cleanupPlan || !selected.length) return original;
  try {
    const { ok, data } = await postCleanup(original, selected);
    if (!ok || !data || !data.applied || !data.upload_id) return original;
    return data.upload_id;
  } catch (e) {
    return original;
  }
}

const PLAN = {
  available: true,
  removable_rows: 1200,
  removable_bytes: 5_000_000,
  default_rule_ids: ['node_traffic_history'],
  rules: [{ id: 'node_traffic_history', rows: 1200, bytes: 5_000_000 }],
};
const OK_ANALYSIS = { ok: true, convert_blocked: false };

function baseState(overrides) {
  return Object.assign(
    {
      restoreUploadId: 'original123',
      cleanupPlan: PLAN,
      cleanupSelected: new Set(['node_traffic_history']),
    },
    overrides || {},
  );
}

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

// ── offering ────────────────────────────────────────────────────────────────

test('offers cleanup when the server reports removable rows', () => {
  assert.strictEqual(shouldOfferCleanup(PLAN), true);
});

test('stays hidden when the feature is disabled', () => {
  assert.strictEqual(shouldOfferCleanup({ available: false, reason: 'disabled' }), false);
});

test('stays hidden when there is nothing to remove', () => {
  assert.strictEqual(shouldOfferCleanup({ available: true, removable_rows: 0 }), false);
});

test('stays hidden when the analyze call produced nothing', () => {
  assert.strictEqual(shouldOfferCleanup(null), false);
  assert.strictEqual(shouldOfferCleanup(undefined), false);
  assert.strictEqual(shouldOfferCleanup({}), false);
});

test('stays hidden while the restore itself is blocked', () => {
  assert.strictEqual(cleanupCardVisible(PLAN, { ok: false }), false);
  assert.strictEqual(cleanupCardVisible(PLAN, { ok: true, convert_blocked: true }), false);
  assert.strictEqual(cleanupCardVisible(PLAN, OK_ANALYSIS), true);
});

// ── resolving the upload id the restore will use ────────────────────────────

test('uses the cleaned upload id on success', async () => {
  const id = await resolveRestoreUploadId(baseState(), async () => ({
    ok: true,
    data: { applied: true, upload_id: 'cleaned456', removed_rows: 1200 },
  }));
  assert.strictEqual(id, 'cleaned456');
});

test('uses the original id when no rule is selected', async () => {
  const state = baseState({ cleanupSelected: new Set() });
  let called = false;
  const id = await resolveRestoreUploadId(state, async () => {
    called = true;
    return { ok: true, data: { applied: true, upload_id: 'cleaned456' } };
  });
  assert.strictEqual(id, 'original123');
  assert.strictEqual(called, false, 'must not call the endpoint with no rules');
});

test('uses the original id when no plan was offered', async () => {
  const state = baseState({ cleanupPlan: null });
  const id = await resolveRestoreUploadId(state, async () => {
    throw new Error('should not be called');
  });
  assert.strictEqual(id, 'original123');
});

test('uses the original id when the server declines', async () => {
  const id = await resolveRestoreUploadId(baseState(), async () => ({
    ok: true,
    data: { applied: false, upload_id: 'original123', reason: 'disk' },
  }));
  assert.strictEqual(id, 'original123');
});

test('uses the original id on an HTTP error', async () => {
  const id = await resolveRestoreUploadId(baseState(), async () => ({
    ok: false,
    data: { detail: 'boom' },
  }));
  assert.strictEqual(id, 'original123');
});

test('uses the original id when the request throws', async () => {
  const id = await resolveRestoreUploadId(baseState(), async () => {
    throw new Error('network down');
  });
  assert.strictEqual(id, 'original123');
});

test('uses the original id on a malformed response', async () => {
  for (const data of [null, {}, { applied: true }, { upload_id: 'x' }]) {
    const id = await resolveRestoreUploadId(baseState(), async () => ({ ok: true, data }));
    assert.strictEqual(id, 'original123', `malformed response accepted: ${JSON.stringify(data)}`);
  }
});

(async () => {
  for (const [name, fn] of cases) {
    await fn();
    console.log(`OK: ${name}`);
  }
  console.log(`\nAll ${cases.length} cleanup UI logic tests passed.`);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
