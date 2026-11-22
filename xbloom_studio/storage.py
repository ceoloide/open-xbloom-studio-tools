"""Local recipe library and append-only execution journal (SQLite, no BLE)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from .migrations import migrate
from .pagination import Pagination, integer, search_text
from .recipes import validate_recipe
from .serialization import digest, dumps, now

__all__ = [
    "ConflictError",
    "NotFoundError",
    "Store",
    "default_db",
    "digest",
    "dumps",
    "now",
]

NotFoundResource = Literal["recipe", "operation", "import"]

ACTIVE_STATUSES = ("accepted", "running", "waiting_for_user")
TERMINAL_STATUSES = ("completed", "failed", "stopped", "unknown")
TRANSITION_STATUSES = ("running", "waiting_for_user", *TERMINAL_STATUSES)
WAL_BUSY_SECONDS = 10


def default_db() -> Path:
    """Default database path under ``$XDG_DATA_HOME`` or ``~/.local/share``."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "xbloom-studio/xbloom-studio.sqlite3"


class ConflictError(ValueError):
    pass


class NotFoundError(KeyError):
    def __init__(self, message: str, *, resource: NotFoundResource) -> None:
        self.resource = resource
        super().__init__(message)


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _busy(error: sqlite3.OperationalError) -> bool:
    return getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY


class Store(Pagination):
    """One local database; immutable revisions and journal, mutable projections.

    A separate application lease prevents two device services using this DB.
    Transactions also serialize recipe-only clients and enforce revision checks.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or default_db()).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA recursive_triggers=ON")
            migrate(self.db)
            deadline = time.monotonic() + WAL_BUSY_SECONDS
            while True:
                try:
                    self.db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if not _busy(error) or time.monotonic() >= deadline:
                        raise
                    # journal_mode can report BUSY immediately during concurrent
                    # opens, even with the connection's busy timeout.
                    time.sleep(0.01)
            self.db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def backup(self, destination: str | os.PathLike[str]) -> dict[str, Any]:
        """Create a consistent standalone copy, without overwriting any file."""
        path = Path(destination).expanduser().absolute()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        try:
            source = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            try:
                target = sqlite3.connect(path)
                try:
                    source.backup(target, pages=128, sleep=0.01)
                finally:
                    target.close()
            finally:
                source.close()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"path": str(path), "consistent_sqlite_backup": True}

    def _check_recipe_name(self, db: sqlite3.Connection, name: str, ident: str) -> None:
        alias = db.execute(
            "SELECT a.recipe_id FROM aliases a JOIN recipes r ON r.id=a.recipe_id "
            "WHERE r.deleted=0 AND a.recipe_id<>? AND a.alias=?",
            (ident, name),
        ).fetchone()
        identifier = db.execute(
            "SELECT id FROM recipes WHERE deleted=0 AND id<>? AND id=?", (ident, name)
        ).fetchone()
        if alias or identifier:
            raise ConflictError("Recipe name collides with another recipe ID or alias")

    def create_recipe(
        self, document: dict[str, Any], temporary: bool = False
    ) -> dict[str, Any]:
        document = validate_recipe(document)
        with self.transaction() as db:
            return self._insert_recipe(db, document, temporary)

    def _insert_recipe(
        self, db: sqlite3.Connection, document: dict[str, Any], temporary: bool
    ) -> dict[str, Any]:
        ident = uuid.uuid4().hex
        self._check_recipe_name(db, document["name"], ident)
        db.execute(
            "INSERT INTO recipes(id,name,head,temporary) VALUES(?,?,1,?)",
            (ident, document["name"], int(temporary)),
        )
        db.execute(
            "INSERT INTO revisions(recipe_id,revision,created_at,document,sha256) "
            "VALUES(?,1,?,?,?)",
            (ident, now(), dumps(document), digest(document)),
        )
        return self.get_recipe(ident)

    def get_recipe(self, reference: str, revision: int | None = None) -> dict[str, Any]:
        """Resolve an ID, then a live alias, then a unique case-insensitive name."""
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM recipes WHERE deleted=0 AND id=?", (reference,)
            ).fetchone()
            if row is None:
                row = self.db.execute(
                    "SELECT * FROM recipes WHERE deleted=0 AND id IN (SELECT "
                    "a.recipe_id FROM aliases a JOIN recipes r ON r.id=a.recipe_id "
                    "WHERE r.deleted=0 AND a.alias=?)",
                    (reference,),
                ).fetchone()
            if row is None:
                matches = self.db.execute(
                    "SELECT * FROM recipes WHERE deleted=0 AND name=? COLLATE NOCASE",
                    (reference,),
                ).fetchall()
                if len(matches) > 1:
                    raise ConflictError("Recipe name is ambiguous; use its ID")
                row = matches[0] if matches else None
            if row is None:
                raise NotFoundError("Recipe not found", resource="recipe")
            version = revision if revision is not None else row["head"]
            rev = self.db.execute(
                "SELECT * FROM revisions WHERE recipe_id=? AND revision=?",
                (row["id"], version),
            ).fetchone()
            if rev is None:
                raise NotFoundError("Recipe revision not found", resource="recipe")
            return {
                **dict(row),
                "revision": version,
                "document": json.loads(rev["document"]),
                "sha256": rev["sha256"],
                "created_at": rev["created_at"],
            }

    def update_recipe(
        self, reference: str, document: dict[str, Any], expected_revision: int
    ) -> dict[str, Any]:
        document = validate_recipe(document)
        with self.transaction() as db:
            current = self.get_recipe(reference)
            if current["head"] != expected_revision:
                raise ConflictError("Recipe changed; reload and merge before updating")
            self._check_recipe_name(db, document["name"], current["id"])
            version = expected_revision + 1
            db.execute(
                "INSERT INTO revisions(recipe_id,revision,created_at,document,sha256) "
                "VALUES(?,?,?,?,?)",
                (current["id"], version, now(), dumps(document), digest(document)),
            )
            db.execute(
                "UPDATE recipes SET name=?,head=? WHERE id=?",
                (document["name"], version, current["id"]),
            )
            return self.get_recipe(current["id"])

    def delete_recipe(self, reference: str, expected_revision: int) -> dict[str, Any]:
        """Archive a recipe; its revisions stay for history and repeat."""
        with self.transaction() as db:
            row = self.get_recipe(reference)
            if row["head"] != expected_revision:
                raise ConflictError("Recipe changed")
            db.execute("UPDATE recipes SET deleted=1 WHERE id=?", (row["id"],))
            db.execute("DELETE FROM aliases WHERE recipe_id=?", (row["id"],))
        return {"id": row["id"], "archived": True}

    def alias(self, reference: str, alias: str) -> dict[str, Any]:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("Alias must be nonempty")
        alias = alias.strip()
        with self.transaction() as db:
            row = self.get_recipe(reference)
            collision = db.execute(
                "SELECT id FROM recipes WHERE deleted=0 AND id<>? "
                "AND (id=? OR name=? COLLATE NOCASE)",
                (row["id"], alias, alias),
            ).fetchone()
            if collision:
                raise ConflictError("Alias collides with another recipe ID or name")
            existing = db.execute(
                "SELECT a.recipe_id FROM aliases a JOIN recipes r ON r.id=a.recipe_id "
                "WHERE r.deleted=0 AND a.alias=?",
                (alias,),
            ).fetchone()
            if existing and existing[0] != row["id"]:
                raise ConflictError("Alias already belongs to another recipe")
            db.execute(
                "INSERT OR REPLACE INTO aliases(alias,recipe_id) VALUES(?,?)",
                (alias, row["id"]),
            )
        return {"alias": alias, "recipe_id": row["id"]}

    def recipes(
        self, query: str = "", include_temporary: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        integer(limit, "limit", 1, 1000)
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM recipes WHERE deleted=0 AND (temporary=0 OR ?) "
                "ORDER BY name",
                (int(include_temporary),),
            ).fetchall()
            result = [self.get_recipe(r["id"]) for r in rows]
        words = query.casefold().split()
        return [
            r for r in result if all(w in search_text(r["document"]) for w in words)
        ][:limit]

    def append(
        self,
        kind: str,
        data: Any,
        *,
        session_id: str | None = None,
        sequence: int | None = None,
        operation_id: str | None = None,
        device: str | None = None,
        at: str | None = None,
        monotonic_ns: int | None = None,
    ) -> int | None:
        with self.transaction() as db:
            return self._insert_event(
                db,
                kind,
                data,
                session_id=session_id,
                sequence=sequence,
                operation_id=operation_id,
                device=device,
                at=at,
                monotonic_ns=monotonic_ns,
            )

    def _insert_event(
        self,
        db: sqlite3.Connection,
        kind: str,
        data: Any,
        *,
        session_id: str | None = None,
        sequence: int | None = None,
        operation_id: str | None = None,
        device: str | None = None,
        at: str | None = None,
        monotonic_ns: int | None = None,
    ) -> int | None:
        cursor = db.execute(
            "INSERT INTO events(event_id,at,monotonic_ns,session_id,sequence,"
            "operation_id,device,kind,data) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                at if at is not None else now(),
                monotonic_ns if monotonic_ns is not None else time.monotonic_ns(),
                session_id,
                sequence,
                operation_id,
                device,
                kind,
                dumps(data),
            ),
        )
        return cursor.lastrowid

    def events(
        self, operation_id: str | None = None, after: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        integer(limit, "limit", 1, 10000)
        integer(after, "after", 0, 2**63 - 1)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE id>? AND (? IS NULL OR operation_id=?) "
                "ORDER BY id LIMIT ?",
                (after, operation_id, operation_id, limit),
            ).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def preset_write_events(self, device: str) -> list[dict[str, Any]]:
        """Single-slot preset write records for one machine, in journal order.

        ``data`` stays undecoded so the reader can fail closed on it.
        """
        with self.lock:
            rows = self.db.execute(
                "SELECT id,at,session_id,sequence,kind,data FROM events WHERE "
                "device=? AND kind GLOB 'preset_write.*' ORDER BY id",
                (device,),
            ).fetchall()
        return [dict(row) for row in rows]

    def session_events_after(
        self, session_id: str, sequence: int, kinds: Sequence[str]
    ) -> list[dict[str, Any]]:
        """One session's events of ``kinds`` after ``sequence``, in order.

        Uses the session index, so it never reads other sessions' history;
        ``data`` stays undecoded.
        """
        marks = ",".join("?" * len(kinds))
        with self.lock:
            rows = self.db.execute(
                "SELECT kind,data FROM events WHERE session_id=? AND sequence>? "
                f"AND kind IN ({marks}) ORDER BY sequence",
                (session_id, sequence, *kinds),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_operation(
        self, device: str, request: Any, plan: dict[str, Any], key: str
    ) -> tuple[dict[str, Any], bool]:
        """Persist an accepted request once per idempotency key.

        Returns the operation and whether it was created. Reusing a key for a
        different device or request is a conflict, never a second operation.
        """
        fingerprint = digest({"device": device, "request": request})
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM operations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise ConflictError(
                        "Idempotency key was used for a different request"
                    )
                return self.operation(existing["id"]), False
            ident = uuid.uuid4().hex
            db.execute(
                "INSERT INTO operations(id,created_at,updated_at,device,status,"
                "request,plan,result,idempotency_key,request_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    now(),
                    now(),
                    device,
                    "accepted",
                    dumps(request),
                    dumps(plan),
                    None,
                    key,
                    fingerprint,
                ),
            )
            self._insert_event(
                db,
                "operation.accepted",
                {"request": request},
                operation_id=ident,
                device=device,
            )
        return self.operation(ident), True

    def operation_by_key(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM operations WHERE idempotency_key=?", (key,)
            ).fetchone()
        return self.operation(row[0]) if row else None

    def operation_lookup(self, idempotency_key: str) -> dict[str, Any]:
        """Read persisted identity only; never acquire machine ownership or start."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("A nonempty idempotency key is required")
        with self.lock:
            row = self.db.execute(
                "SELECT id,device,idempotency_key,status,created_at,updated_at "
                "FROM operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return {"operation": dict(row) if row else None}

    def operation(self, ident: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM operations WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Operation not found", resource="operation")
            result = dict(row)
            for key in ("request", "plan", "result"):
                result[key] = _loads(result[key])
            result["feedback"] = [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM feedback WHERE operation_id=? ORDER BY created_at",
                    (ident,),
                )
            ]
        return result

    def finish(self, ident: str, status: str, result: Any = None) -> None:
        """Record a transition of an accepted operation.

        Only known statuses are accepted, and ``accepted`` is only ever written
        on creation. A terminal outcome, including one recovery recorded as
        ``unknown``, is final: repeating it identically is a no-op, and any
        other transition is a conflict that leaves it unchanged.
        """
        if status not in TRANSITION_STATUSES:
            raise ValueError("status must be one of " + ", ".join(TRANSITION_STATUSES))
        encoded = dumps(result) if result is not None else None
        with self.transaction() as db:
            row = db.execute(
                "SELECT status,result FROM operations WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Operation not found", resource="operation")
            if row["status"] in TERMINAL_STATUSES:
                if (row["status"], row["result"]) == (status, encoded):
                    return
                raise ConflictError(
                    "Operation already has a terminal outcome; it is never overwritten"
                )
            db.execute(
                "UPDATE operations SET status=?,result=?,updated_at=? WHERE id=?",
                (status, encoded, now(), ident),
            )
            self._insert_event(
                db, "operation." + status, {"result": result}, operation_id=ident
            )

    def history(
        self, query: str = "", limit: int = 100, liked: bool = False
    ) -> list[dict[str, Any]]:
        integer(limit, "limit", 1, 1000)
        with self.lock:
            ids = [
                r[0]
                for r in self.db.execute(
                    "SELECT id FROM operations ORDER BY created_at DESC"
                )
            ]
        rows = [self.operation(i) for i in ids]
        words = query.casefold().split()
        return [
            r
            for r in rows
            if all(w in search_text(r) for w in words)
            and (not liked or any((f["rating"] or 0) >= 4 for f in r["feedback"]))
        ][:limit]

    def feedback(
        self, ident: str, note: str, rating: int | None = None
    ) -> dict[str, Any]:
        self.operation(ident)
        if (
            not isinstance(note, str)
            or not note.strip()
            or (
                rating is not None and (type(rating) is not int or not 1 <= rating <= 5)
            )
        ):
            raise ValueError("Feedback needs a note and optional integer rating 1..5")
        result = {
            "id": uuid.uuid4().hex,
            "operation_id": ident,
            "created_at": now(),
            "note": note,
            "rating": rating,
        }
        with self.transaction() as db:
            db.execute(
                "INSERT INTO feedback(id,operation_id,created_at,note,rating) "
                "VALUES(?,?,?,?,?)",
                tuple(result.values()),
            )
            self._insert_event(db, "feedback.added", result, operation_id=ident)
        return result

    def recover(self) -> list[str]:
        """Mark interrupted operations unknown; never resume or retry them.

        Called only by the exclusive owner when it starts, after it holds the
        machine lease, so another live owner's operation is never touched.
        """
        with self.transaction() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM operations WHERE status IN (?,?,?)",
                    ACTIVE_STATUSES,
                )
            ]
            for ident in ids:
                db.execute(
                    "UPDATE operations SET status=?,result=?,updated_at=? WHERE id=?",
                    (
                        "unknown",
                        dumps(
                            {
                                "reason": "Owner exited before recording a terminal "
                                "outcome; do not retry automatically"
                            }
                        ),
                        now(),
                        ident,
                    ),
                )
                self._insert_event(
                    db, "operation.recovered_unknown", {}, operation_id=ident
                )
        return ids

    def save_import(
        self,
        source: dict[str, Any],
        document: dict[str, Any] | None = None,
        error: str | None = None,
        *,
        recipe_temporary: bool | None = None,
    ) -> dict[str, Any]:
        """Retain the source and optionally create a recipe in the same transaction."""
        if recipe_temporary is not None:
            if document is None or error is not None:
                raise ValueError("Recipe creation requires a successful import")
            document = validate_recipe(document)
        ident = uuid.uuid4().hex
        with self.transaction() as db:
            db.execute(
                "INSERT INTO imports(id,created_at,source,document,error) "
                "VALUES(?,?,?,?,?)",
                (
                    ident,
                    now(),
                    dumps(source),
                    dumps(document) if document else None,
                    error,
                ),
            )
            self._insert_event(
                db,
                "recipe.source_imported",
                {
                    "import_id": ident,
                    "source_url": source["url"],
                    "normalization_error": error,
                },
            )
            imported = self.get_import(ident)
            if recipe_temporary is not None and document is not None:
                imported["recipe"] = self._insert_recipe(db, document, recipe_temporary)
            return imported

    def get_import(self, ident: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM imports WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            raise NotFoundError("Source import not found", resource="import")
        result = dict(row)
        for key in ("source", "document"):
            result[key] = _loads(result[key])
        return result
