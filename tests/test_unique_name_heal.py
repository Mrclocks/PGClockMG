"""Tests for duplicate unique-name heal (nodes.name / user_templates.name)."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.unique_name_heal import (
    dedupe_unique_names_sqlite,
    logs_indicate_duplicate_unique_name,
    plan_duplicate_name_renames,
)


def test_plan_keeps_lowest_id_and_renames_rest():
    rows = [
        (3, "usa-reality"),
        (1, "usa-reality"),
        (2, "usa-reality"),
        (4, "eu-node"),
    ]
    renames = plan_duplicate_name_renames(rows)
    assert len(renames) == 2
    by_id = {r[0]: r for r in renames}
    assert by_id[2][2] == "usa-reality-2"
    assert by_id[3][2] == "usa-reality-3"
    assert 1 not in by_id
    print("OK: plan keeps lowest id")


def test_plan_case_insensitive_groups():
    rows = [(1, "USA-Reality"), (2, "usa-reality")]
    renames = plan_duplicate_name_renames(rows, case_insensitive=True)
    assert len(renames) == 1
    assert renames[0][0] == 2
    assert renames[0][2] == "usa-reality-2"
    print("OK: plan case-insensitive")


def test_plan_collision_safe():
    rows = [
        (1, "n"),
        (2, "n"),
        (3, "n-2"),  # would collide with naive rename of id=2
    ]
    renames = plan_duplicate_name_renames(rows)
    assert len(renames) == 1
    assert renames[0][0] == 2
    assert renames[0][2] == "n-2-2"
    print("OK: plan collision-safe")


def test_logs_detector():
    sample = (
        'sqlalchemy.exc.IntegrityError: (asyncmy.errors.IntegrityError) '
        '(1062, "Duplicate entry \'usa-reality\' for key \'nodes.name\'")'
    )
    assert logs_indicate_duplicate_unique_name(sample) is True
    assert logs_indicate_duplicate_unique_name("Access denied for user") is False
    print("OK: logs detector")


def test_sqlite_dedupe_nodes_and_templates():
    import sqlite3

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "db.sqlite3"
        db = sqlite3.connect(str(path))
        db.executescript(
            """
            CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO nodes VALUES (1, 'usa-reality');
            INSERT INTO nodes VALUES (2, 'usa-reality');
            INSERT INTO nodes VALUES (3, 'ok');
            CREATE TABLE user_templates (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO user_templates VALUES (10, 'basic');
            INSERT INTO user_templates VALUES (11, 'basic');
            """
        )
        db.commit()
        db.close()

        n = dedupe_unique_names_sqlite(path)
        assert n == 2
        db = sqlite3.connect(str(path))
        names = sorted(r[0] for r in db.execute("SELECT name FROM nodes").fetchall())
        tnames = sorted(r[0] for r in db.execute("SELECT name FROM user_templates").fetchall())
        db.close()
        assert names == ["ok", "usa-reality", "usa-reality-2"]
        assert tnames == ["basic", "basic-11"]
        # second pass is noop
        assert dedupe_unique_names_sqlite(path) == 0
    print("OK: sqlite dedupe")


if __name__ == "__main__":
    test_plan_keeps_lowest_id_and_renames_rest()
    test_plan_case_insensitive_groups()
    test_plan_collision_safe()
    test_logs_detector()
    test_sqlite_dedupe_nodes_and_templates()
    print("\nAll unique_name_heal tests passed.")
