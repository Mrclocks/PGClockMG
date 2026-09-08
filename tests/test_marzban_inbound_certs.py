"""Tests for optional Marzban inbound TLS cert relocation."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_relocate_copies_acme_paths_into_domain_folder_and_rewrites():
    from app.services.marzban_inbound_certs import relocate_inbound_certs_in_xray_config

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        acme = td_path / "acme" / "vpn.example.com_ecc"
        acme.mkdir(parents=True)
        cert = acme / "fullchain.cer"
        key = acme / "private.key"
        cert.write_text("CERTDATA", encoding="utf-8")
        key.write_text("KEYDATA", encoding="utf-8")

        pgdata = td_path / "pgdata"
        certs_root = pgdata / "certs"
        xray = pgdata / "xray_config.json"
        pgdata.mkdir()
        cfg = {
            "inbounds": [
                {
                    "tag": "vless-tls",
                    "streamSettings": {
                        "security": "tls",
                        "tlsSettings": {
                            "serverName": "vpn.example.com",
                            "certificates": [
                                {
                                    "certificateFile": str(cert),
                                    "keyFile": str(key),
                                }
                            ],
                        },
                    },
                }
            ]
        }
        xray.write_text(json.dumps(cfg), encoding="utf-8")

        logs: list[str] = []
        with patch("app.services.marzban_inbound_certs.PASARGUARD_DATA", pgdata):
            summary = relocate_inbound_certs_in_xray_config(
                xray, certs_root=certs_root, log=logs.append,
            )

        assert summary["copied"] == 1
        assert summary["rewritten"] == 1
        assert "vpn.example.com" in summary["domains"]
        dest_cert = certs_root / "vpn.example.com" / "fullchain.pem"
        dest_key = certs_root / "vpn.example.com" / "privkey.pem"
        assert dest_cert.read_text(encoding="utf-8") == "CERTDATA"
        assert dest_key.read_text(encoding="utf-8") == "KEYDATA"
        mode_key = stat.S_IMODE(dest_key.stat().st_mode)
        mode_cert = stat.S_IMODE(dest_cert.stat().st_mode)
        assert mode_key == 0o600
        assert mode_cert == 0o644

        data = json.loads(xray.read_text(encoding="utf-8"))
        pair = data["inbounds"][0]["streamSettings"]["tlsSettings"]["certificates"][0]
        assert pair["certificateFile"] == "/var/lib/pasarguard/certs/vpn.example.com/fullchain.pem"
        assert pair["keyFile"] == "/var/lib/pasarguard/certs/vpn.example.com/privkey.pem"
    print("OK: relocate acme → domain folder + rewrite")


def test_relocate_is_idempotent():
    from app.services.marzban_inbound_certs import relocate_inbound_certs_in_xray_config

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        pgdata = td_path / "pgdata"
        certs = pgdata / "certs" / "a.example.com"
        certs.mkdir(parents=True)
        (certs / "fullchain.pem").write_text("C", encoding="utf-8")
        (certs / "privkey.pem").write_text("K", encoding="utf-8")
        xray = pgdata / "xray_config.json"
        xray.write_text(
            json.dumps(
                {
                    "inbounds": [
                        {
                            "streamSettings": {
                                "tlsSettings": {
                                    "serverName": "a.example.com",
                                    "certificates": [
                                        {
                                            "certificateFile": str(certs / "fullchain.pem"),
                                            "keyFile": str(certs / "privkey.pem"),
                                        }
                                    ],
                                }
                            }
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with patch("app.services.marzban_inbound_certs.PASARGUARD_DATA", pgdata):
            s1 = relocate_inbound_certs_in_xray_config(xray, certs_root=pgdata / "certs")
            s2 = relocate_inbound_certs_in_xray_config(xray, certs_root=pgdata / "certs")
        assert s1["rewritten"] >= 1
        # Second pass should not fail; paths already under pasarguard certs
        assert s2["missing"] == []
    print("OK: relocate idempotent")


def test_migrator_skips_relocate_when_switch_off():
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    m = MarzbanMigrator(MigrationJob(job_id="off"), {"relocate_inbound_certs": False})
    with patch(
        "app.services.migrators.marzban.relocate_inbound_certs_in_xray_config"
    ) as fn:
        m._maybe_relocate_inbound_certs()
        fn.assert_not_called()
    print("OK: switch off skips relocate")


def test_migrator_runs_relocate_when_switch_on():
    from app.services.migrators.base import MigrationJob
    from app.services.migrators.marzban import MarzbanMigrator

    with tempfile.TemporaryDirectory() as td:
        pgdata = Path(td) / "pgdata"
        pgdata.mkdir()
        xray = pgdata / "xray_config.json"
        xray.write_text('{"inbounds":[]}', encoding="utf-8")
        m = MarzbanMigrator(MigrationJob(job_id="on"), {"relocate_inbound_certs": True})
        with (
            patch("app.services.migrators.marzban.PASARGUARD_DATA", pgdata),
            patch(
                "app.services.migrators.marzban.relocate_inbound_certs_in_xray_config",
                return_value={"copied": 0, "rewritten": 0, "missing": []},
            ) as fn,
        ):
            m._maybe_relocate_inbound_certs()
            fn.assert_called_once()
    print("OK: switch on runs relocate")


def test_restore_request_has_no_relocate_field():
    from app.models import PasarguardRestoreRequest, MigrationRequest

    assert "relocate_inbound_certs" not in PasarguardRestoreRequest.model_fields
    assert "relocate_inbound_certs" in MigrationRequest.model_fields
    req = MigrationRequest(
        source_panel="marzban", source_db="sqlite", target_db="sqlite",
    )
    assert req.relocate_inbound_certs is False
    print("OK: restore model untouched; migrate default off")


def test_strip_json_comments_and_load():
    from app.services.marzban_inbound_certs import load_xray_config

    text = """
    {
      // comment
      "inbounds": [ /* x */ ]
    }
    """
    data = load_xray_config(text)
    assert data["inbounds"] == []
    print("OK: comment strip")


if __name__ == "__main__":
    test_relocate_copies_acme_paths_into_domain_folder_and_rewrites()
    test_relocate_is_idempotent()
    test_migrator_skips_relocate_when_switch_off()
    test_migrator_runs_relocate_when_switch_on()
    test_restore_request_has_no_relocate_field()
    test_strip_json_comments_and_load()
    print("All marzban inbound cert tests passed")
