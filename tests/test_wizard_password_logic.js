/**
 * Node smoke test for wizard password / continue-button logic (mirrors app.js).
 * Run: node tests/test_wizard_password_logic.js
 */

function dbNeedsPassword(db) {
  return db === 'mysql' || db === 'mariadb' || db === 'postgresql' || db === 'timescaledb';
}

function passwordCandidatesConfirmed(rows, confirmed, values) {
  if (!rows.length) return true;
  return rows.every(r => confirmed[r.key] && (values[r.key] ?? r.value ?? '').trim());
}

function getMigrationPassword(db, rows, confirmed, values) {
  const migrationRow = rows.find(r => r.used_for_migration && confirmed[r.key]);
  if (migrationRow) {
    const val = (values[migrationRow.key] ?? migrationRow.value ?? '').trim();
    if (val) return val;
  }
  const order = db === 'mysql' || db === 'mariadb'
    ? ['MYSQL_ROOT_PASSWORD', 'MYSQL_PASSWORD', 'DB_PASSWORD']
    : ['POSTGRES_PASSWORD', 'DB_PASSWORD'];
  for (const key of order) {
    if (confirmed[key] && (values[key] ?? '').trim()) return values[key].trim();
  }
  return null;
}

function hasDbCredentials(db, rows, confirmed, values) {
  if (!dbNeedsPassword(db)) return true;
  if (!passwordCandidatesConfirmed(rows, confirmed, values)) return false;
  return !!getMigrationPassword(db, rows, confirmed, values);
}

function canProceedStep2({ sourceDb, rows, confirmed, values, uploadComplete }) {
  if (!sourceDb) return 'no source db';
  if (dbNeedsPassword(sourceDb) && !hasDbCredentials(sourceDb, rows, confirmed, values)) {
    if (!passwordCandidatesConfirmed(rows, confirmed, values)) return 'password not confirmed';
    return 'creds incomplete';
  }
  if (!uploadComplete) return 'upload incomplete';
  return null;
}

let failed = 0;
function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL:', msg);
    failed++;
  } else {
    console.log('OK:', msg);
  }
}

const rows = [
  { key: 'MYSQL_ROOT_PASSWORD', value: 'rootpass', used_for_migration: true },
  { key: 'DB_PASSWORD', value: 'dbpass', used_for_migration: false },
];

// Before fix this threw ReferenceError: passwordCandidatesConfirmed is not defined
assert(typeof passwordCandidatesConfirmed === 'function', 'passwordCandidatesConfirmed exists');

const emptyConfirmed = {};
const emptyValues = {};
assert(!passwordCandidatesConfirmed(rows, emptyConfirmed, emptyValues), 'unconfirmed passwords block');

const oneConfirmed = { MYSQL_ROOT_PASSWORD: true };
const oneValues = { MYSQL_ROOT_PASSWORD: 'rootpass' };
assert(!passwordCandidatesConfirmed(rows, oneConfirmed, oneValues), 'partial confirm blocks');

const allConfirmed = { MYSQL_ROOT_PASSWORD: true, DB_PASSWORD: true };
const allValues = { MYSQL_ROOT_PASSWORD: 'rootpass', DB_PASSWORD: 'dbpass' };
assert(passwordCandidatesConfirmed(rows, allConfirmed, allValues), 'all passwords confirmed');

assert(
  hasDbCredentials('mysql', rows, allConfirmed, allValues),
  'hasDbCredentials true when migration password confirmed'
);

assert(
  canProceedStep2({
    sourceDb: 'mysql',
    rows,
    confirmed: allConfirmed,
    values: allValues,
    uploadComplete: true,
  }) === null,
  'can proceed step2 after confirm + upload'
);

assert(
  canProceedStep2({
    sourceDb: 'mysql',
    rows,
    confirmed: oneConfirmed,
    values: oneValues,
    uploadComplete: true,
  }) !== null,
  'cannot proceed with only one password confirmed'
);

if (failed) {
  console.error(`\n${failed} test(s) failed`);
  process.exit(1);
}
console.log('\nAll wizard password logic tests passed');
