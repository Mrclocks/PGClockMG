"""Hiddify → PasarGuard experimental user migration."""

import shutil
from pathlib import Path

from app.config import (
    PASARGUARD_DIR, PASARGUARD_DATA, HIDDIFY_DIR, HIDDIFY_MYSQL_PASS,
    BACKUP_DIR, TOOLS_DIR,
)
from app.services.migrators.base import BaseMigrator
from app.services.pasarguard_ops import safe_start_pasarguard


class HiddifyMigrator(BaseMigrator):
    async def run(self, params: dict) -> dict:
        password = params.get("source_db_password")
        upload_path = params.get("upload_path")

        self.job.set_progress(5, "بررسی پیش‌نیازها...")
        if not PASARGUARD_DIR.exists():
            raise RuntimeError("PasarGuard باید قبل از مهاجرت نصب باشد")

        self.job.set_progress(15, "اتصال به دیتابیس Hiddify...")
        mysql_pass = password
        if not mysql_pass and HIDDIFY_MYSQL_PASS.exists():
            mysql_pass = HIDDIFY_MYSQL_PASS.read_text().strip()

        if upload_path:
            users = await self._extract_users_from_upload(upload_path, mysql_pass)
        else:
            users = await self._extract_users_live(mysql_pass)

        if not users:
            raise RuntimeError("هیچ کاربری در دیتابیس Hiddify یافت نشد")

        self.job.log(f"تعداد کاربران یافت‌شده: {len(users)}")

        self.job.set_progress(40, "ایجاد کاربران در PasarGuard...")
        migrated = await self._import_users_to_pasarguard(users)

        self.job.set_progress(90, "راه‌اندازی مجدد PasarGuard...")
        await safe_start_pasarguard(self)

        self.job.set_progress(100, f"مهاجرت آزمایشی انجام شد — {migrated} کاربر منتقل شد")
        return {
            "panel_url": self._get_panel_url(),
            "subscription_preserved": False,
            "users_migrated": migrated,
            "users_total": len(users),
            "warnings_fa": [
                "مهاجرت آزمایشی — تنظیمات inbound و پروتکل منتقل نشده.",
                "لینک‌های اشتراک جدید هستند — به کاربران اطلاع دهید.",
                "محدودیت ترافیک و تاریخ انقضا تا حد امکان منتقل شده.",
            ],
        }

    async def _extract_users_live(self, password: str | None) -> list[dict]:
        if not password:
            raise RuntimeError("رمز دیتابیس MySQL Hiddify لازم است")

        ok, out = await self._run_cmd([
            "mysql", "-u", "hiddifypanel", f"-p{password}",
            "-h", "127.0.0.1", "hiddifypanel", "-e",
            "SELECT name, uuid, usage_limit_GB, package_days, enable, "
            "current_usage_GB, start_date, mode "
            "FROM user WHERE enable=1 OR enable=0;",
        ])
        if not ok:
            # Try root
            ok, out = await self._run_cmd([
                "mysql", "-u", "root", f"-p{password}",
                "-h", "127.0.0.1", "hiddifypanel", "-e",
                "SELECT name, uuid, usage_limit_GB, package_days, enable "
                "FROM user;",
            ])
        if not ok:
            raise RuntimeError(f"اتصال به MySQL Hiddify ناموفق: {out}")

        return self._parse_mysql_output(out)

    async def _extract_users_from_upload(self, upload_path: str, password: str | None) -> list[dict]:
        upload = Path(upload_path)
        work = BACKUP_DIR / self.job.job_id
        work.mkdir(parents=True, exist_ok=True)

        if upload.suffix == ".zip":
            await self._run_cmd(["unzip", "-o", str(upload), "-d", str(work)])
            sql_files = list(work.rglob("*.sql"))
            if not sql_files:
                raise RuntimeError("فایل SQL در zip یافت نشد")
            sql_file = sql_files[0]
        else:
            sql_file = work / upload.name
            shutil.copy2(upload, sql_file)

        # Import to temp sqlite for parsing, or parse SQL directly
        return self._parse_hiddify_sql(sql_file.read_text(encoding="utf-8", errors="ignore"))

    def _parse_mysql_output(self, output: str) -> list[dict]:
        users = []
        lines = output.strip().split("\n")
        if len(lines) < 2:
            return users
        headers = lines[0].split("\t")
        for line in lines[1:]:
            vals = line.split("\t")
            if len(vals) >= 2:
                user = dict(zip(headers, vals))
                users.append({
                    "username": user.get("name", user.get("uuid", "unknown")),
                    "uuid": user.get("uuid", ""),
                    "data_limit_gb": float(user.get("usage_limit_GB", 0) or 0),
                    "expire_days": int(user.get("package_days", 0) or 0),
                    "enabled": user.get("enable", "1") == "1",
                })
        return users

    def _parse_hiddify_sql(self, sql_text: str) -> list[dict]:
        users = []
        import re
        # Parse INSERT INTO user VALUES patterns
        for match in re.finditer(
            r"INSERT INTO [`\"]?user[`\"]?.*?VALUES\s*\((.*?)\);",
            sql_text, re.IGNORECASE | re.DOTALL,
        ):
            # Simplified — extract name-like fields
            vals = match.group(1)
            parts = [p.strip().strip("'\"") for p in vals.split(",")]
            if len(parts) >= 3:
                users.append({
                    "username": parts[1] if len(parts) > 1 else f"user_{len(users)}",
                    "uuid": parts[2] if len(parts) > 2 else "",
                    "data_limit_gb": 0,
                    "expire_days": 30,
                    "enabled": True,
                })
        return users

    async def _import_users_to_pasarguard(self, users: list[dict]) -> int:
        """Import users via pasarguard CLI."""
        migrated = 0
        for user in users:
            username = user["username"]
            data_limit = int(user.get("data_limit_gb", 0) * 1024 * 1024 * 1024)
            expire_days = user.get("expire_days", 30)

            cmd = [
                "pasarguard", "cli", "users", "add",
                "--username", username,
                "--data-limit", str(data_limit),
                "--expire", str(expire_days),
            ]
            if not user.get("enabled", True):
                cmd.extend(["--status", "disabled"])

            ok, out = await self._run_cmd(cmd, timeout=30)
            if ok:
                migrated += 1
                self.job.log(f"✓ کاربر {username} ایجاد شد")
            else:
                self.job.log(f"✗ خطا برای {username}: {out[:100]}")

        return migrated

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
