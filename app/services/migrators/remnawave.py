"""Remnawave → PasarGuard experimental API migration."""

import json
import urllib.request
import urllib.error
from app.config import PASARGUARD_DIR
from app.services.migrators.base import BaseMigrator
from app.services.pasarguard_ops import safe_start_pasarguard


class RemnawaveMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        api_url = (params.get("remnawave_url") or "").rstrip("/")
        api_token = params.get("remnawave_token") or params.get("source_db_password")

        if not api_url or not api_token:
            raise RuntimeError("Remnawave API URL and token are required")

        self.job.set_progress(10, "Checking PasarGuard...")
        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard must be installed before migration")

        self.job.set_progress(20, "Fetching users from Remnawave API...")
        users = await self._fetch_users(api_url, api_token)
        if not users:
            raise RuntimeError("No users found in Remnawave")

        self.job.log(f"Found {len(users)} users")
        self.job.set_progress(40, "Creating users in PasarGuard...")

        migrated = 0
        for user in users:
            username = user.get("username") or user.get("email") or user.get("uuid", "unknown")
            data_limit = int(user.get("trafficLimitBytes") or user.get("data_limit") or 0)
            status = user.get("status", "ACTIVE")
            enabled = status in ("ACTIVE", "active", "enabled", True)

            cmd = [
                "pasarguard", "cli", "users", "add",
                "--username", str(username),
                "--data-limit", str(data_limit),
            ]
            if not enabled:
                cmd.extend(["--status", "disabled"])

            ok, out = await self._run_cmd(cmd, timeout=30)
            if ok:
                migrated += 1
                self.job.log(f"OK: {username}")
            else:
                self.job.log(f"SKIP: {username} — {out[:80]}")

        await safe_start_pasarguard(self)
        self.job.set_progress(100, f"Done — {migrated}/{len(users)} users migrated")

        return {
            "panel_url": self._get_panel_url(),
            "subscription_mode": "changed",
            "users_migrated": migrated,
            "users_total": len(users),
            "warnings": {
                "en": ["Experimental migration. Nodes/inbounds must be configured manually.", "Subscription links changed."],
                "fa": ["مهاجرت آزمایشی. نودها دستی تنظیم شوند.", "لینک اشتراک تغییر کرد."],
                "ru": ["Экспериментальная миграция.", "Ссылки изменились."],
            },
        }

    async def _fetch_users(self, base_url: str, token: str) -> list:
        endpoints = [
            f"{base_url}/api/users",
            f"{base_url}/api/v1/users",
            f"{base_url}/users",
        ]
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        for url in endpoints:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict):
                        for key in ("users", "data", "items", "result"):
                            if key in data and isinstance(data[key], list):
                                return data[key]
                        return [data]
            except urllib.error.HTTPError as e:
                self.job.log(f"API {url}: HTTP {e.code}")
            except Exception as e:
                self.job.log(f"API {url}: {e}")

        raise RuntimeError(
            "Could not fetch users from Remnawave API. "
            "Check URL (e.g. https://panel.example.com) and API token."
        )

    def _get_panel_url(self) -> str:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "SERVER_IP"
        return f"https://{ip}:8000/dashboard/"
