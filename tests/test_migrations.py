"""Schema upgrades from every layout the store could have written.

The layouts are synthetic SQL written for these tests, with different
whitespace and statement order from the module's DDL; no database file or
captured schema is copied. Rows use synthetic values.
"""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - installs the test guards
from xbloom_studio import migrations
from xbloom_studio.storage import Store

TABLES = """
CREATE TABLE recipes(id TEXT PRIMARY KEY, name TEXT NOT NULL, head INTEGER NOT NULL,
  temporary INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE revisions(recipe_id TEXT NOT NULL REFERENCES recipes(id), revision INTEGER
  NOT NULL, created_at TEXT NOT NULL, document TEXT NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY(recipe_id,revision));
CREATE TABLE aliases(alias TEXT PRIMARY KEY COLLATE NOCASE, recipe_id TEXT NOT NULL
  REFERENCES recipes(id));
CREATE TABLE operations(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT
  NOT NULL, device TEXT NOT NULL, status TEXT NOT NULL, request TEXT NOT NULL, plan TEXT
  NOT NULL, result TEXT, idempotency_key TEXT UNIQUE NOT NULL,
  request_hash TEXT NOT NULL);
CREATE TABLE feedback(id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES
  operations(id), created_at TEXT NOT NULL, note TEXT NOT NULL, rating INTEGER);
CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
  at TEXT NOT NULL, monotonic_ns INTEGER NOT NULL, session_id TEXT, sequence INTEGER,
  operation_id TEXT REFERENCES operations(id), device TEXT, kind TEXT NOT NULL,
  data TEXT NOT NULL);
CREATE INDEX event_operation ON events(operation_id,id);
CREATE INDEX event_session ON events(session_id,sequence);
CREATE TRIGGER immutable_events_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER immutable_events_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER immutable_revisions_update BEFORE UPDATE ON revisions
  BEGIN SELECT RAISE(ABORT,'revisions are immutable'); END;
CREATE TRIGGER immutable_revisions_delete BEFORE DELETE ON revisions
  BEGIN SELECT RAISE(ABORT,'revisions are immutable'); END;
"""
TRIGGER = """
CREATE TRIGGER immutable_operation_inputs BEFORE UPDATE OF
  request,plan,device,idempotency_key,request_hash,created_at,id ON operations
  BEGIN SELECT RAISE(ABORT,'accepted operation inputs are immutable'); END;
"""
IMPORTS = """
CREATE TABLE imports(id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  source TEXT NOT NULL, document TEXT, error TEXT);
"""
LAYOUTS = {
    "v1": TABLES + "PRAGMA user_version=1;",
    "v1-trigger": TABLES + TRIGGER + "PRAGMA user_version=1;",
    "v1-imports": IMPORTS + TABLES + TRIGGER + "PRAGMA user_version=1;",
    "v2": IMPORTS + TABLES + TRIGGER + "PRAGMA user_version=2;",
}


class MigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.db"

    def legacy(self, layout="v1"):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(LAYOUTS[layout])
            db.execute("INSERT INTO recipes VALUES('coffee','Old coffee',1,1,0)")
            db.execute(
                "INSERT INTO revisions VALUES('coffee',1,'then',?,'retained-hash')",
                ('{ "name" : "Old coffee", "unknown_future_field": 42 }',),
            )
            db.execute("INSERT INTO aliases VALUES('B','coffee')")
            db.execute(
                "INSERT INTO operations VALUES('brew','then','then','sim',"
                "'waiting_for_user',?,?,NULL,'key','hash')",
                ('{ "intent": "brew" }', '{ "encoder_version": "old", "unknown": 42 }'),
            )
            db.execute("INSERT INTO feedback VALUES('note','brew','then','good',5)")
            for sequence in (1, 2):
                db.execute(
                    "INSERT INTO events "
                    "VALUES(?,?,'then',1,'session',?,'brew','sim','raw',?)",
                    (sequence, str(sequence), sequence, '{ "bytes": "ff00" }'),
                )
            if layout in ("v1-imports", "v2"):
                db.execute(
                    "INSERT INTO imports VALUES('source','then','{}',?,NULL)",
                    ('{ "extra": 1 }',),
                )

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            tables = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT GLOB 'sqlite_*'"
                )
            ]
            return {
                table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in tables
            }

    def pragmas(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return (
                db.execute("PRAGMA user_version").fetchone()[0],
                db.execute("PRAGMA application_id").fetchone()[0],
            )

    def test_every_layout_upgrades_preserving_rows_and_bytes(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                self.path.unlink(missing_ok=True)
                self.legacy(layout)
                before = self.snapshot()
                store = Store(self.path)
                self.assertEqual(
                    store.get_recipe("B")["document"]["unknown_future_field"], 42
                )
                self.assertEqual(store.operation("brew")["status"], "waiting_for_user")
                for sql in (
                    "UPDATE operations SET plan='{}'",
                    "DELETE FROM revisions",
                    "DELETE FROM events",
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        store.db.execute(sql)
                    store.db.rollback()
                store.close()
                self.assertEqual(self.pragmas(), (2, migrations.APPLICATION_ID))
                after = self.snapshot()
                self.assertEqual({t: after[t] for t in before}, before)
                # Reopening neither rewrites rows nor recovers an in-flight brew.
                Store(self.path).close()
                self.assertEqual(self.snapshot(), after)

    def test_fresh_database_matches_the_synthetic_v2_layout(self):
        Store(self.path).close()
        reference = sqlite3.connect(":memory:")
        try:
            reference.executescript(LAYOUTS["v2"])
            with closing(sqlite3.connect(self.path)) as db, db:
                self.assertEqual(migrations._layout(db), migrations._layout(reference))
        finally:
            reference.close()

    def test_failed_upgrade_rolls_back_then_retries(self):
        self.legacy()
        before = self.snapshot()

        def fail(db):
            db.execute("CREATE TABLE partial_upgrade(value TEXT)")
            db.execute("ALTER TABLE recipes ADD COLUMN partial TEXT")
            raise OSError("simulated disk failure")

        with mock.patch.dict(migrations.MIGRATIONS, {2: fail}):
            with self.assertRaisesRegex(OSError, "disk failure"):
                Store(self.path)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.pragmas(), (1, 0))
        Store(self.path).close()
        self.assertEqual(self.snapshot()["operations"], before["operations"])

    def test_concurrent_openers_upgrade_once(self):
        self.legacy("v1-imports")

        def open_store(_):
            store = Store(self.path)
            try:
                return store.append("concurrent", {})
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(open_store, range(4)))
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 6)
        self.assertEqual(self.pragmas()[0], 2)

    def test_new_databases_carry_the_project_application_id(self):
        self.assertEqual(migrations.APPLICATION_ID.to_bytes(4, "big"), b"XBST")
        Store(self.path).close()
        self.assertEqual(self.pragmas(), (2, migrations.APPLICATION_ID))

    def test_incompatible_files_are_refused_unchanged(self):
        for sql, message in (
            ("PRAGMA user_version=99", "newer"),
            ("PRAGMA application_id=12345", "another application"),
            # The identifier written by the former "open-bloom" namespace.
            ("PRAGMA application_id=1329744973", "another application"),
            ("PRAGMA user_version=-1", "invalid version"),
            ("CREATE TABLE unrelated(value TEXT)", "unversioned"),
        ):
            with self.subTest(sql=sql):
                self.path.unlink(missing_ok=True)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(sql)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    Store(self.path)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(Path(str(self.path) + "-wal").exists())

    def test_drift_is_neither_repaired_nor_accepted(self):
        for sql in (
            "DROP TRIGGER immutable_events_delete",
            "CREATE TABLE surprise(value TEXT)",
            "ALTER TABLE recipes ADD COLUMN surprise TEXT",
            "DROP INDEX event_operation",
            "ALTER TABLE events RENAME COLUMN kind TO category",
        ):
            with self.subTest(sql=sql):
                self.path.unlink(missing_ok=True)
                self.legacy("v2")
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(sql)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "supported layout"):
                    Store(self.path)
                self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(
                TABLES.replace("events are append-only", "EVENTS ARE APPEND-ONLY", 1)
                + "PRAGMA user_version=1;"
            )
        with self.assertRaisesRegex(ValueError, "supported layout"):
            Store(self.path)

    def test_statistics_tables_are_not_drift(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ANALYZE")
        Store(self.path).close()

    def test_version_is_rechecked_under_the_writer_lock(self):
        self.legacy()
        original = migrations._validate
        calls = []

        def validate(db):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("schema changed while waiting for lock")
            return original(db)

        with mock.patch.object(migrations, "_validate", side_effect=validate):
            with self.assertRaisesRegex(ValueError, "while waiting"):
                Store(self.path)
        self.assertEqual(self.pragmas()[0], 1)
        Store(self.path).close()

    def test_future_additive_migration_keeps_writers_working(self):
        self.legacy("v2")
        before = self.snapshot()

        def add_field(db):
            db.execute(
                "ALTER TABLE revisions ADD COLUMN "
                "provenance TEXT NOT NULL DEFAULT 'legacy'"
            )

        with (
            mock.patch.object(migrations, "SCHEMA_VERSION", 3),
            mock.patch.dict(migrations.MIGRATIONS, {3: add_field}),
        ):
            store = Store(self.path)
            try:
                row = store.create_recipe({"name": "New", "steps": [{"kind": "tare"}]})
                self.assertEqual(
                    store.update_recipe(
                        row["id"], {"name": "Updated", "steps": [{"kind": "tare"}]}, 1
                    )["revision"],
                    2,
                )
                self.assertEqual(
                    store.db.execute(
                        "SELECT document FROM revisions WHERE recipe_id='coffee'"
                    ).fetchone()[0],
                    before["revisions"][0][3],
                )
            finally:
                store.close()
            Store(self.path).close()
        with self.assertRaisesRegex(ValueError, "newer"):
            Store(self.path)

    def test_post_commit_wal_failure_closes_and_can_retry(self):
        self.legacy()
        original = sqlite3.connect
        connections = []

        class FailingWAL(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == "PRAGMA journal_mode=WAL":
                    raise sqlite3.OperationalError(
                        "simulated WAL configuration failure"
                    )
                return super().execute(sql, *args)

        def connect(path, *args, **kwargs):
            db = original(path, *args, factory=FailingWAL, **kwargs)
            connections.append(db)
            return db

        with mock.patch("xbloom_studio.storage.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "WAL configuration"):
                Store(self.path)
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            connections[0].execute("SELECT 1")
        self.assertEqual(self.pragmas()[0], 2)
        Store(self.path).close()

    def test_transient_wal_busy_retries_until_deadline(self):
        original = sqlite3.connect
        attempts = []
        busy_forever = False

        class BusyWAL(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == "PRAGMA journal_mode=WAL":
                    attempts.append(1)
                    if busy_forever or len(attempts) == 1:
                        error = sqlite3.OperationalError("database is locked")
                        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                        raise error
                return super().execute(sql, *args)

        def connect(path, *args, **kwargs):
            return original(path, *args, factory=BusyWAL, **kwargs)

        with mock.patch("xbloom_studio.storage.sqlite3.connect", side_effect=connect):
            store = Store(self.path)
            self.assertEqual(
                store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )
            store.close()
            self.assertEqual(len(attempts), 2)
            busy_forever = True
            with mock.patch(
                "xbloom_studio.storage.time.monotonic", side_effect=[0, 11]
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    Store(self.path)

            class OtherError(sqlite3.Connection):
                def execute(self, sql, *args):
                    if sql == "PRAGMA journal_mode=WAL":
                        raise sqlite3.OperationalError("disk I/O error")
                    return super().execute(sql, *args)

        with mock.patch(
            "xbloom_studio.storage.sqlite3.connect",
            side_effect=lambda path, *a, **k: original(
                path, *a, factory=OtherError, **k
            ),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
                Store(self.path)
        Store(self.path).close()
