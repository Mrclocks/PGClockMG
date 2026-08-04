"""Hiddify → PasarGuard migration: parse, UUID preserve, redirect mapping."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from app.panels import PANELS  # noqa: E402
from app.services.backup_analyzer import analyze_upload_directory  # noqa: E402
from app.services.migrators.hiddify_lib import (  # noqa: E402
    build_subscription_mapping,
    extract_proxy_paths,
    find_hiddify_json_in_dir,
    is_hiddify_json_backup,
    load_hiddify_json_file,
    normalize_hiddify_users,
    old_hiddify_paths,
    parse_users_from_backup,
    sanitize_username,
    summarize_backup,
)
from app.services.upload_requirements import get_upload_requirements  # noqa: E402
from pg_redirect.mapping import load_path_index, normalize_mapping_file  # noqa: E402
from pg_redirect.server import RedirectApp  # noqa: E402
from pg_redirect.config import ServerConfig  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "hiddify_sample_backup.json"
REAL_UPLOAD = Path(
    "/home/ubuntu/.cursor/projects/workspace/uploads/2026_08_04__15_30_00_e58f.json"
)


@pytest.fixture(scope="module")
def fixture_data():
    assert FIXTURE.is_file(), f"missing fixture {FIXTURE}"
    return load_hiddify_json_file(FIXTURE)


@pytest.fixture(scope="module")
def real_data():
    if not REAL_UPLOAD.is_file():
        pytest.skip("real uploaded Hiddify backup not present in this environment")
    return load_hiddify_json_file(REAL_UPLOAD)


def test_panel_enabled_partial_redirect():
    panel = PANELS["hiddify"]
    assert panel.enabled is True
    assert panel.coming_soon is False
    assert panel.support_level == "partial"
    assert panel.subscription_mode == "redirect"
    # Box copy: group + redirect flow
    assert "hiddify-test" in panel.description["en"]
    assert "ریدایرکت" in panel.description["fa"] or "redirect" in panel.description["en"].lower()
    assert "3x-ui" in panel.description["en"].lower() or "3x-ui" in panel.description["fa"].lower()


def test_upload_requirements_accept_json():
    reqs = get_upload_requirements("hiddify")
    assert reqs["upload_mode"] in ("required", "optional")
    assert reqs.get("allow_zip") is False
    assert reqs.get("allow_separate") is False
    assert len(reqs["slots"]) == 1
    slot = reqs["slots"][0]
    assert slot["id"] == "database"
    assert slot["accept"] == [".json"]
    assert ".sql" not in slot["accept"]
    assert ".zip" not in slot["accept"]


def test_hiddify_json_bundle_upload_accepted():
    from app.services.upload_bundle import init_bundle, save_bundle_slot, _validate_slot_file

    assert _validate_slot_file("database", "export.json", "mysql", panel_id="hiddify") is None
    assert _validate_slot_file("database", "export.json", None, panel_id="hiddify") is None
    err = _validate_slot_file("database", "export.json", "mysql", panel_id="marzban")
    assert err and ".json" in err

    bid = init_bundle()
    raw = FIXTURE.read_bytes()
    result = save_bundle_slot(
        bid, "database", raw, "hiddify_export.json",
        panel_id="hiddify", source_db="mysql",
    )
    assert result.get("ok") is True, result
    assert result.get("error") is None
    bs = result["bundle_status"]
    assert bs["complete"] is True, bs
    assert bs.get("analysis", {}).get("panel_hint") == "hiddify"
    assert bs.get("analysis", {}).get("backup_ok") is True


def test_fixture_is_hiddify_backup(fixture_data):
    assert is_hiddify_json_backup(fixture_data)
    assert len(fixture_data["users"]) >= 8


def test_extract_proxy_paths_from_fixture(fixture_data):
    paths = extract_proxy_paths(fixture_data["hconfigs"])
    assert paths["proxy_path_client"] == "DmPnY3A1UQ9tm58e97"
    assert paths["proxy_path"]
    assert paths["proxy_path_admin"]


def test_normalize_users_unique_and_valid(fixture_data):
    users = normalize_hiddify_users(fixture_data["users"])
    assert len(users) == len(fixture_data["users"])
    names = [u["username"] for u in users]
    assert len(names) == len(set(n.lower() for n in names))
    for u in users:
        assert 3 <= len(u["username"]) <= 128
        assert u["uuid"]
        # PasarGuard username charset
        assert all(c.isalnum() or c in "-_@." for c in u["username"])
        assert "--" not in u["username"] and ".." not in u["username"]


def test_sanitize_username_edge_cases():
    used: set[str] = set()
    a = sanitize_username("", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", used)
    b = sanitize_username("default", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", used)
    c = sanitize_username("default", "cccccccc-cccc-4ccc-8ccc-cccccccccccc", used)
    d = sanitize_username("User Name!!", "dddddddd-dddd-4ddd-8ddd-dddddddddddd", used)
    assert a.startswith("u_")
    assert b == "default"
    assert c != b
    assert " " not in d
    assert "!" not in d


def test_old_hiddify_paths_include_client_and_suffixes():
    paths = old_hiddify_paths("DmPnY3A1UQ9tm58e97", "c6fca10a-6d6c-4b4c-860b-913900dd93cc")
    assert "/DmPnY3A1UQ9tm58e97/c6fca10a-6d6c-4b4c-860b-913900dd93cc" in paths
    assert "/DmPnY3A1UQ9tm58e97/c6fca10a-6d6c-4b4c-860b-913900dd93cc/" in paths
    assert "/DmPnY3A1UQ9tm58e97/c6fca10a-6d6c-4b4c-860b-913900dd93cc/sub/" in paths
    assert "/DmPnY3A1UQ9tm58e97/c6fca10a-6d6c-4b4c-860b-913900dd93cc/all.txt" in paths


def test_build_mapping_and_redirect_lookup(fixture_data):
    users, paths = parse_users_from_backup(fixture_data)
    migrated = []
    for i, u in enumerate(users, start=1):
        migrated.append({
            **u,
            "user_id": i,
            "subscription_url": f"/sub/token-{u['uuid'][:8]}",
        })
    mapping = build_subscription_mapping(
        migrated,
        proxy_path_client=paths["proxy_path_client"],
        proxy_path=paths["proxy_path"],
    )
    assert mapping["panel"] == "hiddify"
    assert mapping["mappings"]
    # Every user has at least primary mapping
    for u in users:
        assert u["username"] in mapping["mappings"]
        entry = mapping["mappings"][u["username"]]
        assert entry["uuid"] == u["uuid"]
        assert entry["new_subscription_url"].startswith("/sub/")
        assert paths["proxy_path_client"] in entry["old_subscription_url"]

    with tempfile.TemporaryDirectory() as tmp:
        map_path = Path(tmp) / "mapping.json"
        map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        normalize_mapping_file(map_path, redirect_base="https://pg.example:8000")
        index = load_path_index(map_path, redirect_base="https://pg.example:8000")
        sample = users[0]
        old = f"/{paths['proxy_path_client']}/{sample['uuid']}"
        assert old in index
        assert index[old] == f"/sub/token-{sample['uuid'][:8]}"
        # trailing slash variant
        assert old + "/" in index or index.get(old + "/") == index[old]

        cfg = ServerConfig(
            host="127.0.0.1",
            port=0,
            redirect_base="https://pg.example:8000",
            pasarguard_env="",
        )
        app = RedirectApp(cfg, index)
        target = app.lookup(old)
        assert target is not None
        assert "token-" in target or target.endswith(sample["uuid"][:8]) or "/sub/" in (
            app.lookup(old) or ""
        )


def test_analyze_upload_detects_hiddify_json(fixture_data):
    with tempfile.TemporaryDirectory() as tmp:
        upload = Path(tmp)
        shutil.copy2(FIXTURE, upload / "hiddify_export.json")
        analysis = analyze_upload_directory(upload)
        assert analysis["panel_hint"] == "hiddify"
        assert analysis["backup_ok"] is True
        assert analysis["detected_source_db"] == "mysql"
        assert analysis["paths"].get("hiddify_json")


def test_find_hiddify_json_in_nested_dir():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        nested = root / "extracted" / "backup"
        nested.mkdir(parents=True)
        shutil.copy2(FIXTURE, nested / "export.json")
        found = find_hiddify_json_in_dir(root)
        assert found is not None
        assert found.name == "export.json"


def test_real_backup_summary_and_uuid_integrity(real_data):
    summary = summarize_backup(real_data)
    assert summary["users_total"] == 237
    assert summary["proxy_path_client"] == "DmPnY3A1UQ9tm58e97"
    users, paths = parse_users_from_backup(real_data)
    assert len(users) == 237
    uuids = [u["uuid"] for u in users]
    assert len(uuids) == len(set(uuids))
    # All usernames unique after sanitize
    assert len({u["username"].lower() for u in users}) == 237
    # Mapping size: primary + extra suffixes per user
    migrated = [
        {**u, "user_id": i, "subscription_url": f"/sub/tok{i}"}
        for i, u in enumerate(users, start=1)
    ]
    mapping = build_subscription_mapping(
        migrated,
        proxy_path_client=paths["proxy_path_client"],
        proxy_path=paths["proxy_path"],
    )
    # At least one mapping key per user
    assert len([k for k in mapping["mappings"] if "#" not in k]) == 237

    with tempfile.TemporaryDirectory() as tmp:
        map_path = Path(tmp) / "m.json"
        map_path.write_text(json.dumps(mapping), encoding="utf-8")
        index = load_path_index(map_path)
        # Spot-check first 20 users resolve
        for u in users[:20]:
            old = f"/{paths['proxy_path_client']}/{u['uuid']}"
            assert old in index, old
            assert index[old].startswith("/sub/")


def test_real_backup_redirect_e2e_sample(real_data):
    """In-process redirect: old Hiddify path → PasarGuard /sub token."""
    users, paths = parse_users_from_backup(real_data)
    u = users[0]
    migrated = [{
        **u,
        "user_id": 1,
        "subscription_url": "/sub/e2e-token-demo",
    }]
    mapping = build_subscription_mapping(
        migrated,
        proxy_path_client=paths["proxy_path_client"],
        proxy_path=paths["proxy_path"],
    )
    with tempfile.TemporaryDirectory() as tmp:
        map_path = Path(tmp) / "m.json"
        map_path.write_text(json.dumps(mapping), encoding="utf-8")
        index = load_path_index(map_path, redirect_base="https://example.com:8000")
        app = RedirectApp(
            ServerConfig(host="127.0.0.1", port=0, redirect_base="https://example.com:8000"),
            index,
        )
        old = f"/{paths['proxy_path_client']}/{u['uuid']}"
        assert app.lookup(old) == "/sub/e2e-token-demo"
        assert app.lookup(old + "/") == "/sub/e2e-token-demo"
        assert app.lookup(old + "/sub/") == "/sub/e2e-token-demo"
        assert app.lookup(old + "/all.txt") == "/sub/e2e-token-demo"


def test_on_hold_when_no_start_date(fixture_data):
    users = normalize_hiddify_users(fixture_data["users"])
    empty = next(u for u in users if u["original_name"] == "" or u["note"] == "empty-name" or u["uuid"].startswith("1111"))
    # Our injected empty-name user has start_date None + package_days 30
    injected = [u for u in users if u["uuid"] == "11111111-1111-4111-8111-111111111111"][0]
    assert injected["status"] == "on_hold"
    assert injected["on_hold_expire_duration"] == 30 * 86400


def test_disabled_user_status(fixture_data):
    users = normalize_hiddify_users(fixture_data["users"])
    disabled = [u for u in users if u["uuid"] == "33333333-3333-4333-8333-333333333333"][0]
    assert disabled["status"] == "disabled"
    assert disabled["enabled"] is False


def test_import_script_is_valid_python_and_hardened():
    """In-container import script must parse and bootstrap /code onto sys.path."""
    import ast

    from app.services.migrators import hiddify_pg_import as imp

    ast.parse(imp.IMPORT_SCRIPT)
    assert imp.HIDDIFY_TEST_GROUP == "hiddify-test"
    assert callable(imp.run_hiddify_user_import)
    for needle in (
        "sys.path.insert",
        "/code",
        "write_result",
        "ensure_hiddify_test_group",
        "hiddify-test",
        "make_sub_token",
        "traceback.format_exc()",
        "UserStatus.disabled",
        "datetime.fromtimestamp",
    ):
        assert needle in imp.IMPORT_SCRIPT, needle

    runner_src = Path(imp.__file__).read_text(encoding="utf-8")
    assert "PYTHONPATH=/code" in runner_src
    assert '"-w", "/code"' in runner_src or "-w\", \"/code\"" in runner_src
    assert "network=container" in runner_src


def test_normalized_users_ready_for_pasarguard_create(real_data):
    """Payload fields must be usable by PasarGuard UserCreate rules."""
    users, _paths = parse_users_from_backup(real_data)
    assert len(users) == 237
    statuses = {u["status"] for u in users}
    assert statuses <= {"active", "on_hold", "disabled"}
    for u in users:
        assert isinstance(u["data_limit"], int)
        assert u["username"] and u["uuid"]
        if u["status"] == "on_hold":
            assert int(u["on_hold_expire_duration"] or 0) > 0
        if u["expire"] is not None:
            assert int(u["expire"]) > 0
