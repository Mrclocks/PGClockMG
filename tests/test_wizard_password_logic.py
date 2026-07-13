"""Smoke test for wizard password / continue-button logic (mirrors app.js)."""


def db_needs_password(db):
    return db in ("mysql", "mariadb", "postgresql", "timescaledb")


def password_candidates_confirmed(rows, confirmed, values):
    if not rows:
        return True
    return all(
        confirmed.get(r["key"]) and (values.get(r["key"]) or r.get("value") or "").strip()
        for r in rows
    )


def get_migration_password(db, rows, confirmed, values):
    for r in rows:
        if r.get("used_for_migration") and confirmed.get(r["key"]):
            val = (values.get(r["key"]) or r.get("value") or "").strip()
            if val:
                return val
    order = (
        ["MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "DB_PASSWORD"]
        if db in ("mysql", "mariadb")
        else ["POSTGRES_PASSWORD", "DB_PASSWORD"]
    )
    for key in order:
        if confirmed.get(key) and (values.get(key) or "").strip():
            return values[key].strip()
    return None


def has_db_credentials(db, rows, confirmed, values):
    if not db_needs_password(db):
        return True
    if not password_candidates_confirmed(rows, confirmed, values):
        return False
    return bool(get_migration_password(db, rows, confirmed, values))


def can_proceed_step2(source_db, rows, confirmed, values, upload_complete):
    if not source_db:
        return "no source db"
    if db_needs_password(source_db) and not has_db_credentials(source_db, rows, confirmed, values):
        if not password_candidates_confirmed(rows, confirmed, values):
            return "password not confirmed"
        return "creds incomplete"
    if not upload_complete:
        return "upload incomplete"
    return None


def test_wizard_password_flow():
    rows = [
        {"key": "MYSQL_ROOT_PASSWORD", "value": "rootpass", "used_for_migration": True},
        {"key": "DB_PASSWORD", "value": "dbpass", "used_for_migration": False},
    ]
    assert not password_candidates_confirmed(rows, {}, {})
    assert not password_candidates_confirmed(
        rows, {"MYSQL_ROOT_PASSWORD": True}, {"MYSQL_ROOT_PASSWORD": "rootpass"}
    )
    all_confirmed = {"MYSQL_ROOT_PASSWORD": True, "DB_PASSWORD": True}
    all_values = {"MYSQL_ROOT_PASSWORD": "rootpass", "DB_PASSWORD": "dbpass"}
    assert password_candidates_confirmed(rows, all_confirmed, all_values)
    assert has_db_credentials("mysql", rows, all_confirmed, all_values)
    assert can_proceed_step2("mysql", rows, all_confirmed, all_values, True) is None
    assert can_proceed_step2(
        "mysql",
        rows,
        {"MYSQL_ROOT_PASSWORD": True},
        {"MYSQL_ROOT_PASSWORD": "rootpass"},
        True,
    ) is not None
    print("OK: wizard password flow")


if __name__ == "__main__":
    test_wizard_password_flow()
    print("All wizard password logic tests passed")
