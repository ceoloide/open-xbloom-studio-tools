import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.test_recipe_encoding import coffee, tea
from xbloom_studio import pagination
from xbloom_studio.recipes import RecipeService, compile_recipe
from xbloom_studio.storage import ConflictError, Store, default_db


def host(name="Water", volume=100):
    return {
        "name": name,
        "coffee": {"name": "Synthetic beans", "origin": "Nowhere"},
        "steps": [{"kind": "water", "volume_ml": volume, "temperature_c": 90}],
    }


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = Store(self.dir / "library" / "xbloom-studio.sqlite3")
        self.addCleanup(self.store.close)


class RecipeStoreTest(StoreTestCase):
    def test_revisions_aliases_names_and_archive(self):
        store = self.store
        self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)
        row = store.create_recipe(host(" Water "))
        self.assertEqual((row["name"], row["head"], row["revision"]), ("Water", 1, 1))
        self.assertEqual(store.get_recipe("water")["id"], row["id"])
        updated = store.update_recipe(row["id"], host("Water", 120), 1)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(
            store.get_recipe(row["id"], 1)["document"]["steps"][0]["volume_ml"], 100
        )
        with self.assertRaisesRegex(ConflictError, "changed"):
            store.update_recipe(row["id"], host("Water", 130), 1)
        with self.assertRaisesRegex(KeyError, "revision"):
            store.get_recipe(row["id"], 9)
        self.assertEqual(
            store.alias(row["id"], " Morning "),
            {"alias": "Morning", "recipe_id": row["id"]},
        )
        self.assertEqual(store.get_recipe("Morning")["id"], row["id"])
        other = store.create_recipe(host("Other"))
        for alias in ("Other", other["id"]):
            with self.assertRaisesRegex(ConflictError, "collides"):
                store.alias(row["id"], alias)
        with self.assertRaisesRegex(ConflictError, "another recipe"):
            store.alias(other["id"], "Morning")
        for name in ("Morning", other["id"]):
            with self.assertRaisesRegex(ConflictError, "collides"):
                store.create_recipe(host(name))
        for bad in ("", " ", None):
            with self.assertRaises(ValueError):
                store.alias(row["id"], bad)
        store.create_recipe(host("other"))
        with self.assertRaisesRegex(ConflictError, "ambiguous"):
            store.get_recipe("OTHER")
        with self.assertRaisesRegex(ConflictError, "changed"):
            store.delete_recipe(row["id"], 1)
        self.assertEqual(
            store.delete_recipe("Morning", 2), {"id": row["id"], "archived": True}
        )
        with self.assertRaises(KeyError):
            store.get_recipe(row["id"])
        # An archived recipe frees its name and alias; its revisions remain.
        store.create_recipe(host("Morning"))
        revisions = store.db.execute(
            "SELECT count(*) FROM revisions WHERE recipe_id=?", (row["id"],)
        )
        self.assertEqual(revisions.fetchone()[0], 2)
        with self.assertRaises(ValueError):
            store.create_recipe({"name": "Bad"})

    def test_listing_search_and_temporary(self):
        store = self.store
        store.create_recipe(host("Bravo"))
        store.create_recipe({**host("Alpha"), "description": "café"}, temporary=True)
        self.assertEqual([r["name"] for r in store.recipes()], ["Bravo"])
        self.assertEqual(
            [r["name"] for r in store.recipes(include_temporary=True)],
            ["Alpha", "Bravo"],
        )
        self.assertEqual(
            [r["name"] for r in store.recipes("CAFÉ", include_temporary=True)],
            ["Alpha"],
        )
        self.assertEqual(len(store.recipes(include_temporary=True, limit=1)), 1)
        with self.assertRaises(ValueError):
            store.recipes(limit=0)

    def test_match_ignores_null_filters_and_metadata_values(self):
        service = RecipeService(self.store)
        self.store.create_recipe(
            {**host("First"), "coffee": {"origin": "Sample A", "roaster": None}}
        )
        expected = self.store.create_recipe(
            {**host("Second"), "coffee": {"origin": "Sample B", "roaster": "Example"}}
        )
        results = service.match({"origin": "Sample B", "roaster": None})
        self.assertEqual([row["id"] for row in results], [expected["id"]])
        self.assertEqual(service.match({"roaster": "none"}), [])

    def test_compare_from_brew_and_match(self):
        service = RecipeService(self.store)
        row = self.store.create_recipe(host("Water"))
        self.store.update_recipe(row["id"], host("Water", 150), 1)
        diff = service.compare(row["id"], 1, 2)
        self.assertTrue(any("150" in line for line in diff["diff"]))
        plan = service.compile(row["id"], 1)
        self.assertEqual((plan["recipe_id"], plan["recipe_revision"]), (row["id"], 1))
        op, _ = self.store.create_operation("sim", {"kind": "recipe"}, plan, "k")
        saved = service.from_brew(op["id"], "Saved")
        self.assertEqual(saved["document"]["source"], {"brew_id": op["id"]})
        self.assertEqual(saved["document"]["steps"][0]["volume_ml"], 100)
        self.store.create_recipe(
            {
                **coffee(),
                "name": "Synthetic",
                "coffee": {"roaster": "Example Roasters", "variety": "Typica"},
            }
        )
        matches = service.match({"name": "synthetic", "variety": "typica"})
        self.assertEqual(
            [m["match_reasons"] for m in matches], [["name"], ["name"], ["variety"]]
        )
        self.assertEqual([m["match_score"] for m in matches], [8, 8, 2])
        self.assertEqual(len(service.match({"origin": "nowhere"}, limit=1)), 1)
        self.assertEqual(service.match({"roaster": "absent"}), [])
        with self.assertRaises(ValueError):
            service.match({"flavor": "x"})
        with self.assertRaises(ValueError):
            service.match({"name": "x"}, limit=0)
        with mock.patch.object(pagination, "BATCH_SIZE", 1):
            pages = []
            original = self.store.recipe_page

            def recipe_page(**kwargs):
                kwargs["limit"] = 1
                page = original(**kwargs)
                pages.append(page)
                return page

            with mock.patch.object(self.store, "recipe_page", side_effect=recipe_page):
                self.assertEqual(len(service.match({"origin": "nowhere"})), 2)
            self.assertGreater(len(pages), 1)


class OperationStoreTest(StoreTestCase):
    def test_idempotent_operations_feedback_history_and_recovery(self):
        store = self.store
        plan = compile_recipe(tea())
        op, created = store.create_operation(
            "sim", {"kind": "recipe", "n": 1}, plan, "key-1"
        )
        self.assertTrue(created)
        self.assertEqual(op["status"], "accepted")
        with self.assertRaisesRegex(ConflictError, "different request"):
            store.create_operation("sim", {"kind": "recipe", "n": 2}, plan, "key-1")
        with self.assertRaisesRegex(ConflictError, "different request"):
            store.create_operation("other", {"kind": "recipe", "n": 1}, plan, "key-1")
        self.assertEqual(store.operation_by_key("key-1")["id"], op["id"])
        self.assertIsNone(store.operation_by_key("missing"))
        lookup = store.operation_lookup("key-1")["operation"]
        self.assertEqual(
            set(lookup),
            {"id", "device", "idempotency_key", "status", "created_at", "updated_at"},
        )
        self.assertEqual(store.operation_lookup("absent"), {"operation": None})
        for bad in ("", " ", None):
            with self.assertRaises(ValueError):
                store.operation_lookup(bad)
        with self.assertRaises(sqlite3.IntegrityError):
            store.db.execute("UPDATE operations SET plan='{}' WHERE id=?", (op["id"],))
        store.db.rollback()
        note = store.feedback(op["id"], "Bright", 5)
        self.assertEqual(note["rating"], 5)
        for args in (("",), ("x", 6), ("x", True), ("x", 4.0)):
            with self.assertRaises(ValueError):
                store.feedback(op["id"], *args)
        with self.assertRaises(KeyError):
            store.feedback("missing", "x")
        done, _ = store.create_operation(
            "sim", {"kind": "recipe", "n": 3}, plan, "key-2"
        )
        store.finish(done["id"], "completed", {"ok": True})
        self.assertEqual(store.operation(done["id"])["result"], {"ok": True})
        third, _ = store.create_operation(
            "sim", {"kind": "recipe", "n": 4}, plan, "key-3"
        )
        store.finish(third["id"], "stopped")
        self.assertEqual([o["id"] for o in store.history(liked=True)], [op["id"]])
        self.assertEqual(len(store.history("bright")), 1)
        self.assertEqual(len(store.history(limit=1)), 1)
        self.assertEqual(store.recover(), [op["id"]])
        recovered = store.operation(op["id"])
        self.assertEqual(recovered["status"], "unknown")
        self.assertIn("do not retry", recovered["result"]["reason"])
        self.assertEqual(store.recover(), [])
        kinds = [e["kind"] for e in store.events(op["id"])]
        self.assertEqual(
            kinds,
            ["operation.accepted", "feedback.added", "operation.recovered_unknown"],
        )
        with self.assertRaises(KeyError):
            store.operation("missing")

    def test_events_are_append_only_and_bounded(self):
        store = self.store
        first = store.append(
            "sample", {"value": 1}, session_id="s", sequence=1, device="sim"
        )
        store.append(
            "sample", {"value": 2}, at="2026-01-01T00:00:00+00:00", monotonic_ns=5
        )
        self.assertEqual([e["data"]["value"] for e in store.events(after=first)], [2])
        self.assertEqual(store.events(limit=1)[0]["session_id"], "s")
        for kwargs in ({"limit": 0}, {"after": -1}, {"limit": True}):
            with self.assertRaises(ValueError):
                store.events(**kwargs)
        for sql in (
            "UPDATE events SET kind='x'",
            "DELETE FROM events",
            "INSERT OR REPLACE INTO events(event_id, kind, at, monotonic_ns, data) "
            "SELECT event_id, kind, at, monotonic_ns, data FROM events LIMIT 1",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(sql)
            store.db.rollback()

    def test_interrupted_transaction_leaves_no_partial_rows(self):
        store = self.store
        with mock.patch.object(
            store, "_insert_event", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.create_operation(
                    "sim", {"kind": "recipe"}, compile_recipe(tea()), "k"
                )
            with self.assertRaises(OSError):
                store.save_import({"url": None}, error="x")
        self.assertIsNone(store.operation_by_key("k"))
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0
        )
        with self.assertRaises(KeyboardInterrupt), store.transaction() as db:
            db.execute("INSERT INTO recipes VALUES('x','x',1,0,0)")
            raise KeyboardInterrupt
        self.assertEqual(
            store.db.execute("SELECT count(*) FROM recipes").fetchone()[0], 0
        )

    def test_imports_and_backup(self):
        store = self.store
        blocked = store.save_import(
            {"url": "https://example.com/r", "raw": {"a": 1}}, error="no"
        )
        self.assertEqual((blocked["error"], blocked["document"]), ("no", None))
        saved = store.save_import({"url": None}, document=host())
        self.assertEqual(store.get_import(saved["id"])["document"]["name"], "Water")
        with self.assertRaises(KeyError):
            store.get_import("missing")
        imported = [e for e in store.events() if e["kind"] == "recipe.source_imported"]
        self.assertEqual(imported[0]["data"]["normalization_error"], "no")
        store.create_recipe(host())
        target = self.dir / "copy.db"
        self.assertEqual(
            store.backup(target),
            {"path": str(target), "consistent_sqlite_backup": True},
        )
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        copy = Store(target)
        try:
            self.assertEqual(copy.get_recipe("Water")["name"], "Water")
        finally:
            copy.close()
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            store.backup(target)
        self.assertEqual(target.read_bytes(), before)
        failing = self.dir / "failing.db"
        with mock.patch(
            "xbloom_studio.storage.sqlite3.connect",
            side_effect=sqlite3.OperationalError("no"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                store.backup(failing)
        self.assertFalse(failing.exists())

    def test_two_connections_serialize_revisions(self):
        other = Store(self.store.path)
        self.addCleanup(other.close)
        row = self.store.create_recipe(host())
        results = []

        def update(store, volume):
            try:
                results.append(
                    store.update_recipe(row["id"], host("Water", volume), 1)["revision"]
                )
            except ConflictError:
                results.append("conflict")

        threads = [
            threading.Thread(target=update, args=(s, v))
            for s, v in ((self.store, 110), (other, 120))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results, key=str), [2, "conflict"])
        self.assertEqual(other.get_recipe(row["id"])["head"], 2)

    def test_store_forces_full_synchronous_writes(self):
        # Start below FULL so the test fails if the store stops raising it.
        original = sqlite3.connect
        lowered = []

        def connect(path, *args, **kwargs):
            db = original(path, *args, **kwargs)
            db.execute("PRAGMA synchronous=NORMAL")
            lowered.append(db)
            return db

        with mock.patch("xbloom_studio.storage.sqlite3.connect", side_effect=connect):
            store = Store(self.dir / "durable.sqlite3")
        self.addCleanup(store.close)
        self.assertIn(store.db, lowered)
        self.assertEqual(store.db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_default_path_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.dir)}):
            self.assertEqual(
                default_db(), self.dir / "xbloom-studio/xbloom-studio.sqlite3"
            )
        with (
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}),
            mock.patch.object(Path, "home", return_value=self.dir / "home"),
        ):
            self.assertEqual(
                default_db(),
                self.dir / "home/.local/share/xbloom-studio/xbloom-studio.sqlite3",
            )

    def test_default_store_ignores_a_database_at_the_former_path(self):
        data = self.dir / "xdg"
        former = data / "open-bloom/bloom.sqlite3"
        former.parent.mkdir(parents=True)
        decoy = Store(former)
        decoy.create_recipe(host("Decoy"))
        decoy.close()
        before = {p.name: p.read_bytes() for p in former.parent.iterdir()}
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
            store = Store()
            try:
                self.assertEqual(store.recipes(), [])
            finally:
                store.close()
        self.assertTrue((data / "xbloom-studio/xbloom-studio.sqlite3").exists())
        after = {p.name: p.read_bytes() for p in former.parent.iterdir()}
        self.assertEqual(after, before)


class PaginationTest(StoreTestCase):
    def test_recipe_pages_are_stable_and_filtered(self):
        rows = [
            self.store.create_recipe(host(f"R{i}"), temporary=i == 3) for i in range(5)
        ]
        ids = sorted(r["id"] for r in rows if r["name"] != "R3")
        page = self.store.recipe_page(limit=2)
        self.assertEqual([i["id"] for i in page["items"]], ids[:2])
        self.assertEqual(
            set(page["items"][0]),
            {"id", "name", "head", "revision", "temporary", "sha256", "created_at"},
        )
        self.assertTrue(page["has_more"])
        seen = [i["id"] for i in page["items"]]
        while page["has_more"]:
            page = self.store.recipe_page(limit=2, cursor=page["next_cursor"])
            seen += [i["id"] for i in page["items"]]
        self.assertEqual(seen, ids)
        self.assertIsNone(page["next_cursor"])
        full = self.store.recipe_page(query="r1", detail="full", include_temporary=True)
        self.assertEqual([i["name"] for i in full["items"]], ["R1"])
        self.assertIn("document", full["items"][0])
        self.assertEqual(
            len(self.store.recipe_page(include_temporary=True)["items"]), 5
        )
        cursor = self.store.recipe_page(limit=1)["next_cursor"]
        with self.assertRaisesRegex(ValueError, "filters changed"):
            self.store.recipe_page(limit=1, cursor=cursor, query="x")
        with self.assertRaisesRegex(ValueError, "filters changed"):
            self.store.history_page(cursor=cursor)
        empty = self.store.recipe_page(query="absent")
        self.assertEqual((empty["items"], empty["has_more"]), ([], False))

    def test_invalid_arguments_and_cursors(self):
        store = self.store
        for kwargs in (
            {"limit": 0},
            {"query": 1},
            {"query": "x" * 1025},
            {"include_temporary": 1},
            {"detail": "all"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.recipe_page(**kwargs)

        def encode(value):
            raw = json.dumps(value).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        filters = {"query": "", "include_temporary": False}
        for cursor in (
            "",
            "!",
            "x" * 16385,
            "aGVsbG8",
            encode([]),
            encode(
                {"v": 2, "resource": "recipes", "filters": filters, "position": ["a"]}
            ),
            encode(
                {"v": 1, "resource": "recipes", "filters": filters, "position": "a"}
            ),
            encode({"v": 1, "resource": "recipes", "filters": filters, "position": []}),
            encode(
                {"v": 1, "resource": "recipes", "filters": filters, "position": [""]}
            ),
            5,
        ):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                store.recipe_page(cursor=cursor)
        event_filters = {"operation_id": None}
        for position in ([], [0], ["1"]):
            cursor = encode(
                {
                    "v": 1,
                    "resource": "events",
                    "filters": event_filters,
                    "position": position,
                }
            )
            with self.assertRaises(ValueError):
                store.event_page(cursor=cursor)
        history_filters = {
            "query": "",
            "liked": False,
            "status": None,
            "min_rating": None,
        }
        for position in (
            [],
            ["x" * 65, "a"],
            ["not a date", "a"],
            ["2026-01-01", ""],
            [1, "a"],
        ):
            cursor = encode(
                {
                    "v": 1,
                    "resource": "history",
                    "filters": history_filters,
                    "position": position,
                }
            )
            with self.assertRaises(ValueError):
                store.history_page(cursor=cursor)
        for kwargs in (
            {"status": "done"},
            {"min_rating": 6},
            {"liked": "yes"},
            {"limit": 1001},
        ):
            with self.assertRaises(ValueError):
                store.history_page(**kwargs)
        for kwargs in (
            {"operation_id": ""},
            {"operation_id": "x" * 129},
            {"after": -1},
            {"limit": 10001},
        ):
            with self.assertRaises(ValueError):
                store.event_page(**kwargs)
        cursor = store.event_page(limit=1)["next_cursor"]
        self.assertIsNone(cursor)
        store.append("a", {})
        store.append("b", {})
        cursor = store.event_page(limit=1)["next_cursor"]
        with self.assertRaisesRegex(ValueError, "either"):
            store.event_page(after=1, cursor=cursor)

    def test_event_cursor_id_upper_bound(self):
        def cursor(event_id):
            value = {
                "v": 1,
                "resource": "events",
                "filters": {"operation_id": None},
                "position": [event_id],
            }
            raw = json.dumps(value).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        page = self.store.event_page(cursor=cursor(9223372036854775807))
        self.assertEqual((page["items"], page["has_more"]), ([], False))
        with self.assertRaisesRegex(
            ValueError,
            "^cursor event ID must be an integer from 1 to 9223372036854775807$",
        ):
            self.store.event_page(cursor=cursor(9223372036854775808))

    def test_history_pages_filters_and_compact_projection(self):
        store = self.store
        plan = {**compile_recipe(tea()), "recipe_id": "r", "recipe_revision": 1}
        ops = []
        for index in range(4):
            with mock.patch(
                "xbloom_studio.storage.now",
                return_value=f"2026-01-0{index + 1}T00:00:00+00:00",
            ):
                op, _ = store.create_operation("sim", {"n": index}, plan, f"k{index}")
            ops.append(op["id"])
        for index in range(7):
            store.feedback(ops[1], f"note {index} floral", 5 if index == 6 else 2)
        store.feedback(ops[2], "fine", 3)
        store.finish(ops[3], "failed", {"error": "x"})
        page = store.history_page(limit=2)
        self.assertEqual([i["id"] for i in page["items"]], [ops[3], ops[2]])
        rest = store.history_page(limit=2, cursor=page["next_cursor"])
        self.assertEqual([i["id"] for i in rest["items"]], [ops[1], ops[0]])
        self.assertEqual(rest["ordering"], "created_at_id_desc")
        compact = rest["items"][0]
        self.assertEqual(
            (compact["recipe_name"], compact["rating"], compact["feedback_count"]),
            ("Synthetic tea", 5, 7),
        )
        self.assertTrue(compact["feedback_truncated"])
        self.assertEqual([n["note"] for n in compact["feedback"]][-1], "note 6 floral")
        self.assertEqual(len(compact["feedback"]), 5)
        self.assertEqual(
            [i["id"] for i in store.history_page(liked=True)["items"]], [ops[1]]
        )
        self.assertEqual(
            [i["id"] for i in store.history_page(min_rating=3)["items"]],
            [ops[2], ops[1]],
        )
        self.assertEqual(
            [i["id"] for i in store.history_page(status="failed")["items"]], [ops[3]]
        )
        self.assertEqual(
            [i["id"] for i in store.history_page(query="note 0 floral")["items"]],
            [ops[1]],
        )
        self.assertEqual(
            [i["id"] for i in store.history_page(query="synthetic n")["items"]],
            ops[::-1],
        )
        self.assertEqual(store.history_page(query="nothing")["items"], [])
        full = store.history_page(detail="full", limit=1)["items"][0]
        self.assertIn("plan", full)
        self.assertEqual(
            store.history_page(detail="full", query="floral")["items"][0]["id"], ops[1]
        )

    def test_event_pages_and_scan_budget(self):
        store = self.store
        for index in range(5):
            store.append("sample", {"i": index}, operation_id=None)
        page = store.event_page(limit=2, detail="compact")
        self.assertNotIn("data", page["items"][0])
        ids = [i["id"] for i in page["items"]]
        page = store.event_page(limit=2, cursor=page["next_cursor"])
        ids += [i["id"] for i in page["items"]]
        page = store.event_page(after=ids[-1])
        ids += [i["id"] for i in page["items"]]
        self.assertEqual(len(ids), 5)
        self.assertEqual(page["items"][0]["data"], {"i": 4})
        with (
            mock.patch.object(pagination, "SCAN_BUDGET", 2),
            mock.patch.object(pagination, "BATCH_SIZE", 1),
        ):
            scan = store.recipe_page(query="absent")
            self.assertEqual(scan["items"], [])
            store.create_recipe(host("A"))
            store.create_recipe(host("B"))
            store.create_recipe(host("C"))
            scan = store.recipe_page(query="absent")
            self.assertTrue(scan["scan_exhausted"])
            self.assertTrue(scan["has_more"])
            self.assertIsNotNone(scan["next_cursor"])
