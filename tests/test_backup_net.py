"""SSRF / proxy host guards for backup panel networking."""

from __future__ import annotations

import pytest

from app.services.backup_net import (
    UnsafeDestinationError,
    assert_proxy_host,
    assert_public_hostname,
    normalize_public_http_url,
)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "10.0.0.1",
        "10.255.255.255",
        "169.254.169.254",
    ],
)
def test_assert_public_hostname_blocks_private_and_metadata(host: str):
    with pytest.raises(UnsafeDestinationError):
        assert_public_hostname(host)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://127.0.0.1:8080/path",
        "https://localhost/backup",
        "http://10.1.2.3",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_normalize_public_http_url_blocks_unsafe_literals(url: str):
    with pytest.raises(UnsafeDestinationError):
        normalize_public_http_url(url)


def test_assert_public_hostname_blocks_localhost_aliases():
    with pytest.raises(UnsafeDestinationError):
        assert_public_hostname("foo.localhost")
    with pytest.raises(UnsafeDestinationError):
        assert_public_hostname("metadata.google.internal")


def test_assert_proxy_host_rejects_scheme_in_host():
    with pytest.raises(UnsafeDestinationError):
        assert_proxy_host("http://evil.example")
    with pytest.raises(UnsafeDestinationError):
        assert_proxy_host("socks5://127.0.0.1")


def test_assert_proxy_host_allows_plain_host():
    assert_proxy_host("proxy.example.com")
    assert_proxy_host("192.168.1.1")
