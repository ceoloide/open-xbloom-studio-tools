"""Bounded keyset reads of live local collections; no storage schema changes."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

BATCH_SIZE = 128
SCAN_BUDGET = 4096
MAX_CURSOR_LENGTH = 16384
HISTORY_STATUSES = (
    "accepted",
    "running",
    "waiting_for_user",
    "completed",
    "stopped",
    "failed",
    "unknown",
)
ORDERING = {
    "recipes": "id_asc",
    "history": "created_at_id_desc",
    "events": "event_id_asc",
}

Row = sqlite3.Row
Position = list[Any]


def integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _query(value: object) -> str:
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError("query must be a string of at most 1024 characters")
    return " ".join(value.casefold().split())


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(name + " must be a boolean")
    return value


def _detail(value: object) -> None:
    if value not in ("compact", "full"):
        raise ValueError("detail must be compact or full")


def _identifier(value: object, name: str, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError(name + " must be a nonempty string of at most 128 characters")


def _position(resource: str, position: object) -> None:
    if not isinstance(position, list):
        raise ValueError("Invalid pagination cursor position")
    if resource == "events":
        if len(position) != 1:
            raise ValueError("Invalid event cursor position")
        integer(position[0], "cursor event ID", 1, 2**63 - 1)
    elif resource == "recipes":
        if len(position) != 1:
            raise ValueError("Invalid recipe cursor position")
        _identifier(position[0], "cursor recipe ID")
    else:
        if (
            len(position) != 2
            or not isinstance(position[0], str)
            or len(position[0]) > 64
        ):
            raise ValueError("Invalid history cursor position")
        try:
            datetime.fromisoformat(position[0])
        except ValueError as exc:
            raise ValueError("Invalid history cursor timestamp") from exc
        _identifier(position[1], "cursor operation ID")


def _cursor(resource: str, filters: dict[str, Any], position: Position) -> str:
    value = {"v": 1, "resource": resource, "filters": filters, "position": position}
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _read_cursor(
    value: object, resource: str, filters: dict[str, Any]
) -> Position | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_CURSOR_LENGTH
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise ValueError("Invalid pagination cursor; restart the collection scan")
    try:
        decoded = json.loads(
            base64.b64decode(
                value + "=" * ((-len(value)) % 4), altchars=b"-_", validate=True
            )
        )
    except (binascii.Error, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(
            "Invalid pagination cursor; restart the collection scan"
        ) from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"v", "resource", "filters", "position"}
        or type(decoded["v"]) is not int
        or decoded["v"] != 1
        or decoded["resource"] != resource
        or _json(decoded["filters"]) != _json(filters)
    ):
        raise ValueError(
            "Cursor version, collection or filters changed; restart the collection scan"
        )
    _position(resource, decoded["position"])
    position: Position = decoded["position"]
    return position


def _page(
    resource: str,
    filters: dict[str, Any],
    position: Position | None,
    limit: int,
    fetch: Callable[[Position | None, int], Sequence[Row]],
    key: Callable[[Row], Position],
    project: Callable[[Row], dict[str, Any]],
    matches: Callable[[Row], bool],
) -> dict[str, Any]:
    """Process at most SCAN_BUDGET rows, plus a one-candidate continuation probe.

    has_more means unscanned candidates remain, not a promised next match. A
    post-filtered page may be empty; its cursor still advances. Projection and
    page size are not membership filters and can change between requests.
    """
    items: list[dict[str, Any]] = []
    scanned = 0
    while scanned < SCAN_BUDGET and len(items) < limit:
        rows = fetch(position, min(BATCH_SIZE, SCAN_BUDGET - scanned))
        if not rows:
            break
        for row in rows:
            position = key(row)
            scanned += 1
            if matches(row):
                items.append(project(row))
            if len(items) == limit:
                break
    has_more = bool(fetch(position, 1)) if position is not None else False
    return {
        "items": items,
        "count": len(items),
        "limit": limit,
        "has_more": has_more,
        "next_cursor": _cursor(resource, filters, position)
        if has_more and position is not None
        else None,
        "scan_exhausted": scanned == SCAN_BUDGET and has_more,
        "ordering": ORDERING[resource],
        "live_collection": True,
    }


def search_text(value: Any) -> str:
    """Search human text, while canonical storage and hashes keep ASCII escapes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).casefold()


def _matches(query: str, value: Any) -> bool:
    text = search_text(value)
    return all(word in text for word in query.split())


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Pagination:
    """Store mixin. The lock protects each bounded read, not a multi-page scan."""

    db: sqlite3.Connection
    lock: threading.RLock

    if TYPE_CHECKING:

        def operation(self, ident: str) -> dict[str, Any]: ...

    def recipe_page(
        self,
        query: str = "",
        include_temporary: bool = False,
        limit: int = 100,
        cursor: str | None = None,
        detail: str = "compact",
    ) -> dict[str, Any]:
        integer(limit, "limit", 1, 1000)
        query = _query(query)
        _boolean(include_temporary, "include_temporary")
        _detail(detail)
        filters = {"query": query, "include_temporary": include_temporary}
        position = _read_cursor(cursor, "recipes", filters)

        def fetch(after: Position | None, size: int) -> list[Row]:
            continuation = " AND r.id>?" if after else ""
            parameters: tuple[Any, ...] = (
                (include_temporary, after[0], size)
                if after
                else (include_temporary, size)
            )
            return self.db.execute(
                "SELECT r.*,v.revision,v.created_at,v.document,v.sha256 FROM recipes r "
                "JOIN revisions v ON v.recipe_id=r.id AND v.revision=r.head "
                "WHERE r.deleted=0 AND (r.temporary=0 OR ?)"
                + continuation
                + " ORDER BY r.id LIMIT ?",
                parameters,
            ).fetchall()

        def full(row: Row) -> dict[str, Any]:
            return {**dict(row), "document": json.loads(row["document"])}

        def project(row: Row) -> dict[str, Any]:
            if detail == "full":
                return full(row)
            return {
                name: row[name]
                for name in (
                    "id",
                    "name",
                    "head",
                    "revision",
                    "temporary",
                    "sha256",
                    "created_at",
                )
            }

        with self.lock:
            return _page(
                "recipes",
                filters,
                position,
                limit,
                fetch,
                lambda row: [row["id"]],
                project,
                lambda row: not query or _matches(query, full(row)),
            )

    def history_page(
        self,
        query: str = "",
        liked: bool = False,
        status: str | None = None,
        min_rating: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
        detail: str = "compact",
    ) -> dict[str, Any]:
        integer(limit, "limit", 1, 1000)
        query = _query(query)
        _boolean(liked, "liked")
        _detail(detail)
        if status is not None and status not in HISTORY_STATUSES:
            raise ValueError(
                "status must be accepted, running, waiting_for_user, completed, "
                "stopped, failed or unknown"
            )
        if min_rating is not None:
            integer(min_rating, "min_rating", 1, 5)
        filters = {
            "query": query,
            "liked": liked,
            "status": status,
            "min_rating": min_rating,
        }
        position = _read_cursor(cursor, "history", filters)
        minimum = max(4 if liked else 0, min_rating or 0)
        cache: dict[str, dict[str, Any]] = {}

        def fetch(after: Position | None, size: int) -> list[Row]:
            at, ident = (after[0], after[1]) if after else (None, None)
            return self.db.execute(
                "SELECT id,created_at FROM operations WHERE (? IS NULL OR status=?) "
                "AND (?=0 OR EXISTS (SELECT 1 FROM feedback f "
                "WHERE f.operation_id=operations.id AND f.rating>=?)) "
                "AND (? IS NULL OR created_at<? OR (created_at=? AND id<?)) "
                "ORDER BY created_at DESC,id DESC LIMIT ?",
                (status, status, minimum, minimum, at, at, at, ident, size),
            ).fetchall()

        def full(row: Row) -> dict[str, Any]:
            if row["id"] not in cache:
                cache.clear()  # Keep only the current candidate, not the corpus.
                cache[row["id"]] = self.operation(row["id"])
            return cache[row["id"]]

        def base(row: Row) -> dict[str, Any]:
            value = dict(
                self.db.execute(
                    "SELECT * FROM operations WHERE id=?", (row["id"],)
                ).fetchone()
            )
            for name in ("request", "plan", "result"):
                value[name] = _loads(value[name])
            value["feedback"] = []
            return value

        def matches(row: Row) -> bool:
            if not query:
                return True
            # Stream older feedback rather than building an unbounded list.
            # The scan budget bounds candidates, not SQLite query time.
            text = search_text(base(row))
            remaining = {word for word in query.split() if word not in text}
            if not remaining:
                return True
            for note in self.db.execute(
                "SELECT * FROM feedback WHERE operation_id=?", (row["id"],)
            ):
                text = search_text(dict(note))
                remaining = {word for word in remaining if word not in text}
                if not remaining:
                    return True
            return False

        def project(row: Row) -> dict[str, Any]:
            if detail == "full":
                return full(row)
            value = base(row)
            plan = value["plan"]
            stats = self.db.execute(
                "SELECT COUNT(*),MAX(rating) FROM feedback WHERE operation_id=?",
                (row["id"],),
            ).fetchone()
            feedback = [
                dict(note)
                for note in self.db.execute(
                    "SELECT * FROM feedback WHERE operation_id=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 5",
                    (row["id"],),
                )
            ]
            feedback.reverse()
            return {
                **{
                    name: value[name]
                    for name in ("id", "created_at", "updated_at", "status")
                },
                "recipe_id": plan.get("recipe_id"),
                "recipe_revision": plan.get("recipe_revision"),
                "recipe_name": plan.get("recipe_snapshot", {}).get("name"),
                "recipe_sha256": plan.get("recipe_sha256"),
                "rating": stats[1],
                "feedback": feedback,
                "feedback_count": stats[0],
                "feedback_truncated": stats[0] > 5,
            }

        with self.lock:
            return _page(
                "history",
                filters,
                position,
                limit,
                fetch,
                lambda row: [row["created_at"], row["id"]],
                project,
                matches,
            )

    def event_page(
        self,
        operation_id: str | None = None,
        after: int = 0,
        limit: int = 1000,
        cursor: str | None = None,
        detail: str = "full",
    ) -> dict[str, Any]:
        integer(limit, "limit", 1, 10000)
        integer(after, "after", 0, 2**63 - 1)
        _identifier(operation_id, "operation_id", optional=True)
        _detail(detail)
        if cursor is not None and after:
            raise ValueError("Use either after or cursor, not both")
        filters = {"operation_id": operation_id}
        position = _read_cursor(cursor, "events", filters) or (
            [after] if after else None
        )
        columns = (
            "*"
            if detail == "full"
            else "id,event_id,at,monotonic_ns,session_id,sequence,"
            "operation_id,device,kind"
        )

        def fetch(last: Position | None, size: int) -> list[Row]:
            return self.db.execute(
                "SELECT " + columns + " FROM events WHERE id>? "
                "AND (? IS NULL OR operation_id=?) ORDER BY id LIMIT ?",
                (last[0] if last else 0, operation_id, operation_id, size),
            ).fetchall()

        def project(row: Row) -> dict[str, Any]:
            value = dict(row)
            if detail == "full":
                value["data"] = json.loads(value["data"])
            return value

        with self.lock:
            return _page(
                "events",
                filters,
                position,
                limit,
                fetch,
                lambda row: [row["id"]],
                project,
                lambda row: True,
            )
