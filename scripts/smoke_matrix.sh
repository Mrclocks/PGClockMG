#!/usr/bin/env bash
# Phase 0.4 — unit smoke matrix (no Docker). Exit non-zero on first failure.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

echo "== smoke: native migration / strategy matrix =="
python3 tests/test_native_migration.py

echo "== smoke: marzban migrate matrix =="
python3 tests/test_marzban_migrate_matrix.py

echo "== smoke: marzban inbound guards =="
python3 tests/test_marzban_inbound_guards.py

echo "== smoke: marzban preboot heal =="
python3 tests/test_marzban_preboot_heal.py

echo "== smoke: cross-db strategy matrix =="
python3 tests/test_cross_db_matrix.py

echo "== smoke: x-ui migrator =="
python3 tests/test_xui_migrator.py

echo "== smoke: hiddify migrator =="
python3 -m pytest tests/test_hiddify_migrator.py -q

echo "== smoke: soft skip / restore contract =="
python3 tests/test_soft_user_skip_and_lock.py
python3 tests/test_cleanup_api.py

echo "All smoke_matrix suites passed."
