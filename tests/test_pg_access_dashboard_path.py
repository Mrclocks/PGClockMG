"""DASHBOARD_PATH must drive panel login URLs (not a hardcoded /dashboard/)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.pg_access import (
    build_dashboard_url,
    normalize_dashboard_path,
    resolve_dashboard_path,
)
from app.services.env_migration import get_panel_url_from_env


def test_normalize_dashboard_path():
    assert normalize_dashboard_path(None) == "/dashboard/"
    assert normalize_dashboard_path("") == "/dashboard/"
    assert normalize_dashboard_path("secret") == "/secret/"
    assert normalize_dashboard_path("/panel") == "/panel/"
    assert normalize_dashboard_path("/panel/") == "/panel/"
    assert normalize_dashboard_path('"/my-admin/"') == "/my-admin/"
    print("OK: normalize_dashboard_path")


def test_resolve_dashboard_path_prefers_dashboard_path():
    env = 'DASHBOARD_PATH = "/my-panel/"\nUVICORN_ROOT_PATH = "/ignored/"\n'
    assert resolve_dashboard_path(env) == "/my-panel/"
    print("OK: DASHBOARD_PATH wins over ROOT_PATH")


def test_resolve_dashboard_path_legacy_root_path():
    env = 'UVICORN_ROOT_PATH = "/prefix"\n'
    assert resolve_dashboard_path(env) == "/prefix/dashboard/"
    print("OK: legacy UVICORN_ROOT_PATH → /prefix/dashboard/")


def test_resolve_dashboard_path_default():
    assert resolve_dashboard_path("") == "/dashboard/"
    assert resolve_dashboard_path("UVICORN_PORT = 8000\n") == "/dashboard/"
    print("OK: default /dashboard/")


def test_build_dashboard_url_uses_dashboard_path():
    url = build_dashboard_url("panel.example.com", 8000, https=True, dashboard_path="/admin/")
    assert url == "https://panel.example.com:8000/admin/"
    url2 = build_dashboard_url("10.0.0.1", "8000", https=False, dashboard_path="secret")
    assert url2 == "http://10.0.0.1:8000/secret/"
    print("OK: build_dashboard_url with DASHBOARD_PATH")


def test_build_dashboard_url_legacy_root_path():
    url = build_dashboard_url("host.example", 8000, root_path="/x")
    assert url == "https://host.example:8000/x/dashboard/"
    print("OK: build_dashboard_url legacy root_path")


def test_get_panel_url_from_env_reads_dashboard_path():
    env = 'UVICORN_PORT = 8443\nDASHBOARD_PATH = "/pg/"\n'
    url = get_panel_url_from_env(env, ip="203.0.113.10")
    assert url == "https://203.0.113.10:8443/pg/"
    print("OK: get_panel_url_from_env uses DASHBOARD_PATH")


if __name__ == "__main__":
    test_normalize_dashboard_path()
    test_resolve_dashboard_path_prefers_dashboard_path()
    test_resolve_dashboard_path_legacy_root_path()
    test_resolve_dashboard_path_default()
    test_build_dashboard_url_uses_dashboard_path()
    test_build_dashboard_url_legacy_root_path()
    test_get_panel_url_from_env_reads_dashboard_path()
