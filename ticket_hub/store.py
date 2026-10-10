"""ticket_hub store — SQLite (WAL), append-only events, single writer.

The Store is the only writer of hub state. Events are immutable: UPDATE/DELETE
are blocked by DB triggers and there are no update/delete APIs for events.
Ticket rows denormalize ``status``/``updated_at`` for cheap reads; the event
chain remains the source of truth for history.

Stdlib only. Python 3.12+. See DESIGN.md for the contract.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .model import (
    Actor,
    Edge,
    Event,
    Status,
    SYSTEM_ACTOR,
    ThreadBinding,
    Ticket,
    assert_transition,
    coerce_status,
    new_id,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS actors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    family TEXT NOT NULL DEFAULT 'unknown',
    kind TEXT NOT NULL DEFAULT 'agent'
);

CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    owner_id TEXT REFERENCES actors(id),
    tags TEXT NOT NULL DEFAULT '[]',
    families TEXT NOT NULL DEFAULT '[]',
    external_refs TEXT NOT NULL DEFAULT '{}',
    artifacts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    src TEXT NOT NULL REFERENCES tickets(id),
    dst TEXT NOT NULL REFERENCES tickets(id),
    type TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (src, dst, type)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    ts TEXT NOT NULL,
    actor_id TEXT,
    actor_json TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events (ticket_id, seq);

-- Append-only enforcement: no updates, no deletes, ever.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only: update forbidden');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only: delete forbidden');
END;

CREATE TABLE IF NOT EXISTS thread_bindings (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    platform TEXT NOT NULL,
    room_id TEXT NOT NULL,
    thread_id TEXT,
    root_msg_id TEXT,
    created_at TEXT NOT NULL
);
"""


class Store:
    """Single-writer SQLite store for the ticket graph.

    ``Store(path)`` opens (creating if needed) a WAL-mode database at *path*.
    Pass ``":memory:"`` for tests.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    # -- actors -------------------------------------------------------------

    def ensure_actor(self, actor: Actor | None) -> Actor:
        """Upsert *actor* (identity by id) and return it. None -> SYSTEM_ACTOR."""
        if actor is None:
            actor = SYSTEM_ACTOR
        self._conn.execute(
            """INSERT INTO actors (id, name, family, kind) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, family=excluded.family, kind=excluded.kind""",
            (actor.id, actor.name, actor.family, actor.kind),
        )
        return actor

    def get_actor(self, actor_id: str) -> Actor | None:
        row = self._conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
        return Actor(**dict(row)) if row else None

    # -- tickets ------------------------------------------------------------

    def create_ticket(
        self,
        title: str,
        body: str = "",
        parent: str | None = None,
        owner: Actor | None = None,
        tags: list[str] | None = None,
        priority: str = "normal",
        families: list[str] | None = None,
        external_refs: dict | None = None,
        artifacts: list | None = None,
        actor: Actor | None = None,
    ) -> Ticket:
        """Create a ticket; when *parent* (a ticket id) is given, link it with
        a ``parent_of`` edge. Raises KeyError if the parent does not exist and
        ValueError on a cycle (impossible for a fresh child; guarded anyway).
        Emits a ``created`` event."""
        if not title or not title.strip():
            raise ValueError("ticket title must be non-empty")
        if parent is not None:
            if self.get_ticket(parent) is None:
                raise KeyError(f"parent ticket not found: {parent}")
        owner = self.ensure_actor(owner)
        tid = new_id("TK")
        now = _utcnow()
        families = list(families or [])
        if owner.family and owner.family not in families:
            families.append(owner.family)
        self._conn.execute(
            """INSERT INTO tickets (id, title, body, status, priority, owner_id,
                                    tags, families, external_refs, artifacts,
                                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                title,
                body,
                Status.DRAFT.value,
                priority,
                owner.id,
                json.dumps(list(tags or [])),
                json.dumps(families),
                json.dumps(external_refs or {}),
                json.dumps(artifacts or []),
                now,
                now,
            ),
        )
        if parent is not None:
            self._insert_edge(parent, tid, "parent_of")
        self._append_event(
            tid,
            "created",
            actor=actor or owner,
            payload={"title": title, "parent": parent, "tags": list(tags or [])},
        )
        self._conn.commit()
        ticket = self.get_ticket(tid)
        assert ticket is not None
        return ticket

    def get_ticket(self, ticket_id: str) -> "Ticket | None":
        row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return self._row_to_ticket(row) if row else None

    def _require_ticket(self, ticket_id: str) -> Ticket:
        t = self.get_ticket(ticket_id)
        if t is None:
            raise KeyError(f"ticket not found: {ticket_id}")
        return t

    # Public alias used by projections (lineage.py) and lanes B/C.
    require_ticket = _require_ticket

    def list_tickets(
        self,
        status: "Status | str | None" = None,
        family: str | None = None,
        tag: str | None = None,
        q: str | None = None,
    ) -> "list[Ticket]":
        """Filter by status, family (owner family or declared families),
        tag, and/or free-text q (title/body/id substring)."""
        sql = ["SELECT * FROM tickets WHERE 1=1"]
        args: list = []
        if status is not None:
            sql.append("AND status = ?")
            args.append(coerce_status(status).value)
        if family is not None:
            sql.append(
                """AND (owner_id IN (SELECT id FROM actors WHERE family = ?)
                        OR EXISTS (SELECT 1 FROM json_each(families) WHERE value = ?))"""
            )
            args.extend([family, family])
        if tag is not None:
            sql.append("AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)")
            args.append(tag)
        if q is not None:
            sql.append("AND (title LIKE ? OR body LIKE ? OR id LIKE ?)")
            like = f"%{q}%"
            args.extend([like, like, like])
        sql.append("ORDER BY created_at ASC, rowid ASC")
        rows = self._conn.execute(" ".join(sql), args).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def update_ticket(
        self,
        ticket_id: str,
        status: "Status | str | None" = None,
        priority: str | None = None,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        external_refs: dict | None = None,
        artifacts: list | None = None,
        actor: Actor | None = None,
    ) -> "Ticket":
        """Patch mutable fields. A status change must be an allowed transition
        (ValueError otherwise) and emits ``status_changed``; other field
        changes emit ``updated``. Bumps denormalized ``updated_at``."""
        t = self._require_ticket(ticket_id)
        changes: dict = {}
        new_status = None
        if status is not None:
            new_status = coerce_status(status)
            if new_status is not t.status:
                assert_transition(t.status, new_status)
                changes["status"] = {"from": t.status.value, "to": new_status.value}
        if priority is not None and priority != t.priority:
            changes["priority"] = {"from": t.priority, "to": priority}
        if title is not None and title != t.title:
            if not title.strip():
                raise ValueError("ticket title must be non-empty")
            changes["title"] = {"from": t.title, "to": title}
        if body is not None and body != t.body:
            changes["body"] = {"to_length": len(body)}
        if tags is not None and tags != t.tags:
            changes["tags"] = {"from": t.tags, "to": list(tags)}
        if external_refs is not None:
            changes["external_refs"] = {"to_keys": sorted(external_refs)}
        if artifacts is not None and artifacts != t.artifacts:
            changes["artifacts"] = {"from": t.artifacts, "to": list(artifacts)}

        if changes:
            now = _utcnow()
            self._conn.execute(
                """UPDATE tickets SET
                     title = COALESCE(?, title),
                     body = COALESCE(?, body),
                     status = COALESCE(?, status),
                     priority = COALESCE(?, priority),
                     tags = COALESCE(?, tags),
                     external_refs = COALESCE(?, external_refs),
                     artifacts = COALESCE(?, artifacts),
                     updated_at = ?
                   WHERE id = ?""",
                (
                    title,
                    body,
                    new_status.value if new_status is not None else None,
                    priority,
                    json.dumps(tags) if tags is not None else None,
                    json.dumps(external_refs) if external_refs is not None else None,
                    json.dumps(artifacts) if artifacts is not None else None,
                    now,
                    ticket_id,
                ),
            )
            if "status" in changes:
                self._append_event(
                    ticket_id, "status_changed", actor=actor,
                    payload={"from": changes["status"]["from"], "to": changes["status"]["to"]},
                )
            other = {k: v for k, v in changes.items() if k != "status"}
            if other:
                self._append_event(ticket_id, "updated", actor=actor, payload={"fields": sorted(other)})
            self._conn.commit()
        return self._require_ticket(ticket_id)

    # -- edges / graph --------------------------------------------------------

    def _insert_edge(self, src: str, dst: str, type: str, meta: dict | None = None) -> Edge:
        if type not in {"parent_of", "blocks", "relates_to", "duplicate_of"}:
            raise ValueError(f"unknown edge type: {type!r}")
        self._require_ticket(src)
        self._require_ticket(dst)
        if src == dst:
            raise ValueError(f"self-edge rejected: {src} -> {dst}")
        if type == "parent_of":
            existing = self._conn.execute(
                "SELECT src FROM edges WHERE dst = ? AND type = 'parent_of'", (dst,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"{dst} already has a parent: {existing['src']}")
            # cycle check: dst must not already be an ancestor of src
            if dst in self._ancestry_ids(src):
                raise ValueError(f"cycle rejected: {dst} is already an ancestor of {src}")
        cur = self._conn.execute(
            "INSERT INTO edges (id, src, dst, type, meta, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("ED"), src, dst, type, json.dumps(meta or {}), _utcnow()),
        )
        row = self._conn.execute("SELECT * FROM edges WHERE rowid = ?", (cur.lastrowid,)).fetchone()
        return self._row_to_edge(row)

    def add_edge(self, src: str, dst: str, type: str, meta: dict | None = None) -> Edge:
        """Add an edge. Rejects self-edges, unknown types, duplicate
        ``(src, dst, type)``, a second parent, and ``parent_of`` cycles
        (ValueError)."""
        dupe = self._conn.execute(
            "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND type = ?", (src, dst, type)
        ).fetchone()
        if dupe is not None:
            raise ValueError(f"edge already exists: {src} -[{type}]-> {dst}")
        edge = self._insert_edge(src, dst, type, meta)
        self._append_event(
            src, "link", actor=SYSTEM_ACTOR,
            payload={"edge": edge.to_dict()},
        )
        self._conn.commit()
        return edge

    def get_edges(
        self, ticket_id: str, type: str | None = None, direction: str = "both"
    ) -> "list[Edge]":
        """Edges touching *ticket_id*. direction: 'out' | 'in' | 'both'."""
        self._require_ticket(ticket_id)
        clauses, args = [], []
        if direction in ("out", "both"):
            clauses.append("src = ?")
            args.append(ticket_id)
        if direction in ("in", "both"):
            clauses.append("dst = ?")
            args.append(ticket_id)
        sql = f"SELECT * FROM edges WHERE ({' OR '.join(clauses)})"
        if type is not None:
            sql += " AND type = ?"
            args.append(type)
        sql += " ORDER BY created_at ASC, rowid ASC"
        return [self._row_to_edge(r) for r in self._conn.execute(sql, args).fetchall()]

    def parent_of(self, ticket_id: str) -> "Ticket | None":
        row = self._conn.execute(
            "SELECT t.* FROM edges e JOIN tickets t ON t.id = e.src "
            "WHERE e.dst = ? AND e.type = 'parent_of'",
            (ticket_id,),
        ).fetchone()
        return self._row_to_ticket(row) if row else None

    def children(self, ticket_id: str) -> "list[Ticket]":
        rows = self._conn.execute(
            "SELECT t.* FROM edges e JOIN tickets t ON t.id = e.dst "
            "WHERE e.src = ? AND e.type = 'parent_of' ORDER BY e.created_at, e.rowid",
            (ticket_id,),
        ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def _ancestry_ids(self, ticket_id: str) -> "list[str]":
        """Parent ids walking upward; nearest parent first. Guarded against
        pre-existing corruption with a visited set."""
        ids: list[str] = []
        seen = {ticket_id}
        current = ticket_id
        while True:
            parent = self.parent_of(current)
            if parent is None:
                return ids
            if parent.id in seen:
                raise ValueError(f"corrupt hierarchy: cycle detected at {parent.id}")
            ids.append(parent.id)
            seen.add(parent.id)
            current = parent.id

    def ancestry(self, ticket_id: str) -> "list[Ticket]":
        """Ancestors root-first (breadcrumb order: root ... parent)."""
        self._require_ticket(ticket_id)
        return [self._require_ticket(pid) for pid in reversed(self._ancestry_ids(ticket_id))]

    def descendants(self, ticket_id: str) -> "list[Ticket]":
        """All descendants, BFS order (depth ascending, then creation order)."""
        self._require_ticket(ticket_id)
        out: list[Ticket] = []
        queue = self.children(ticket_id)
        seen: set[str] = set()
        while queue:
            nxt: list[Ticket] = []
            for t in queue:
                if t.id in seen:
                    continue
                seen.add(t.id)
                out.append(t)
                nxt.extend(self.children(t.id))
            queue = nxt
        return out

    def related(self, ticket_id: str) -> "list[Ticket]":
        """Tickets joined by non-hierarchy edges (blocks/relates_to/duplicate_of),
        either direction, deduplicated."""
        self._require_ticket(ticket_id)
        rows = self._conn.execute(
            "SELECT t.* FROM edges e JOIN tickets t "
            "  ON t.id = CASE WHEN e.src = ? THEN e.dst ELSE e.src END "
            "WHERE (e.src = ? OR e.dst = ?) "
            "  AND e.type IN ('blocks', 'relates_to', 'duplicate_of') "
            "ORDER BY e.created_at, e.rowid",
            (ticket_id, ticket_id, ticket_id),
        ).fetchall()
        seen: set[str] = set()
        out: list[Ticket] = []
        for r in rows:
            t = self._row_to_ticket(r)
            if t.id not in seen:
                seen.add(t.id)
                out.append(t)
        return out

    # -- events ---------------------------------------------------------------

    def append_event(
        self, ticket_id: str, kind: str, actor: Actor | None = None, payload: dict | None = None
    ) -> Event:
        """Append an immutable event to *ticket_id*. There is no API to update
        or delete events; DB triggers enforce this even for raw SQL."""
        self._require_ticket(ticket_id)
        ev = self._append_event(ticket_id, kind, actor=actor, payload=payload)
        self._conn.commit()
        return ev

    def _append_event(
        self, ticket_id: str, kind: str, actor: Actor | None = None, payload: dict | None = None
    ) -> Event:
        actor = self.ensure_actor(actor)
        ts = _utcnow()
        ev_id = new_id("EV")
        payload = dict(payload or {})
        cur = self._conn.execute(
            "INSERT INTO events (id, ticket_id, ts, actor_id, actor_json, kind, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ev_id, ticket_id, ts, actor.id, json.dumps(actor.to_dict()), kind, json.dumps(payload)),
        )
        return Event(
            id=ev_id, ticket_id=ticket_id, ts=ts, actor=actor, kind=kind,
            payload=payload, seq=cur.lastrowid or 0,
        )

    def events(self, ticket_id: str) -> "list[Event]":
        """Full event chain for a ticket, ordered by (ts, seq)."""
        self._require_ticket(ticket_id)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE ticket_id = ? ORDER BY ts ASC, seq ASC", (ticket_id,)
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # -- thread bindings --------------------------------------------------------

    def bind_thread(
        self,
        ticket_id: str,
        platform: str,
        room_id: str,
        thread_id: str | None = None,
        root_msg_id: str | None = None,
    ) -> ThreadBinding:
        self._require_ticket(ticket_id)
        binding = ThreadBinding(
            id=new_id("TB"), ticket_id=ticket_id, platform=platform,
            room_id=room_id, thread_id=thread_id, root_msg_id=root_msg_id,
        )
        self._conn.execute(
            "INSERT INTO thread_bindings (id, ticket_id, platform, room_id, thread_id, root_msg_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (binding.id, ticket_id, platform, room_id, thread_id, root_msg_id, _utcnow()),
        )
        self._append_event(
            ticket_id, "link", actor=SYSTEM_ACTOR,
            payload={"bound_thread": binding.to_dict()},
        )
        self._conn.commit()
        return binding

    def bindings(self, ticket_id: str) -> "list[ThreadBinding]":
        self._require_ticket(ticket_id)
        rows = self._conn.execute(
            "SELECT * FROM thread_bindings WHERE ticket_id = ? ORDER BY created_at, rowid",
            (ticket_id,),
        ).fetchall()
        return [
            ThreadBinding(
                id=r["id"], ticket_id=r["ticket_id"], platform=r["platform"],
                room_id=r["room_id"], thread_id=r["thread_id"], root_msg_id=r["root_msg_id"],
            )
            for r in rows
        ]

    # -- projections --------------------------------------------------------------

    def board(self) -> "dict[str, list[Ticket]]":
        """Tickets grouped by status value (all statuses present, possibly empty)."""
        grouped: dict[str, list[Ticket]] = {s.value: [] for s in Status}
        for t in self.list_tickets():
            grouped[t.status.value].append(t)
        return grouped

    def timeline(self, ticket_id: str) -> "list[dict]":
        """Digest-friendly event list: the ticket's own events merged with
        ``thread_message`` events from any ticket sharing one of its thread
        bindings (same platform + room + thread). Each entry is an event dict
        plus ``source_ticket``; ordered by (ts, seq)."""
        t = self._require_ticket(ticket_id)
        own = [
            {**ev.to_dict(), "source_ticket": ticket_id} for ev in self.events(ticket_id)
        ]
        foreign: list[dict] = []
        for b in self.bindings(ticket_id):
            peers = self._conn.execute(
                "SELECT ticket_id FROM thread_bindings "
                "WHERE platform = ? AND room_id = ? "
                "  AND IFNULL(thread_id, '') = IFNULL(?, '') AND ticket_id != ?",
                (b.platform, b.room_id, b.thread_id, ticket_id),
            ).fetchall()
            for p in peers:
                for ev in self.events(p["ticket_id"]):
                    if ev.kind == "thread_message":
                        foreign.append({**ev.to_dict(), "source_ticket": p["ticket_id"]})
        merged = own + foreign
        merged.sort(key=lambda e: (e["ts"], e["seq"]))
        return merged

    # -- row mappers ------------------------------------------------------------------

    def _row_to_ticket(self, row: sqlite3.Row) -> "Ticket":
        owner = None
        if row["owner_id"]:
            owner = self.get_actor(row["owner_id"])
        return Ticket(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            status=Status(row["status"]),
            priority=row["priority"],
            owner=owner,
            tags=json.loads(row["tags"]),
            families=json.loads(row["families"]),
            external_refs=json.loads(row["external_refs"]),
            artifacts=json.loads(row["artifacts"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_edge(self, row: sqlite3.Row) -> Edge:
        return Edge(
            id=row["id"], src=row["src"], dst=row["dst"], type=row["type"],
            meta=json.loads(row["meta"]),
        )

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        actor = None
        if row["actor_json"]:
            actor = Actor(**json.loads(row["actor_json"]))
        return Event(
            id=row["id"], ticket_id=row["ticket_id"], ts=row["ts"], actor=actor,
            kind=row["kind"], payload=json.loads(row["payload"]), seq=row["seq"],
        )
