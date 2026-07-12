"""Native cross-database migration (no PasarGuard compose alembic / db-migrations tool)."""

from app.services.native_migration.cross_db import run_native_cross_db_migration

__all__ = ["run_native_cross_db_migration"]
