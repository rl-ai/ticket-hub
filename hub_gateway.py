"""Gateway: adapts the core lane's ``ticket_hub.store.Store`` (dataclass API,
DESIGN signatures ``get_ticket/list_tickets/update_ticket``) to the shape the
api lane codes against (dict-returning ``get/list/patch/get_binding``).

Single writer per DESIGN non-goal: one Store per process, FastAPI runs
requests against it; sqlite3 connection uses check_same_thread default False
below so uvicorn worker threads share it with serialized access.
"""
from __future__ import annotations

from typing import Any, Optional

from ticket_hub.model import Actor, Status, coerce_status
from ticket_hub.store import Store


def _to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def _actor(raw: Any) -> Optional[Actor]:
    if raw is None:
        return None
    if isinstance(raw, Actor):
        return raw
    if isinstance(raw, str):
        return Actor(id=raw, name=raw)
    if isinstance(raw, dict):
        name = str(raw.get("name") or raw.get("id") or "unknown")
        return Actor(
            id=str(raw.get("id") or name),
            name=name,
            family=str(raw.get("family") or "unknown"),
            kind=str(raw.get("kind") or "agent"),
        )
    raise ValueError(f"cannot coerce actor: {raw!r}")


class StoreGateway:
    """Dict-surfacing facade over the real Store (contract: DESIGN.md)."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # -- pass-through mutating calls, dict-ifying results ------------------

    def create_ticket(self, *, title: str, body: str = "", parent: Optional[str] = None,
                      owner: Any = None, tags: Optional[list] = None,
                      families: Optional[list] = None, priority: str = "normal",
                      external_refs: Optional[dict] = None, **kw) -> dict:
        return _to_dict(self._store.create_ticket(
            title=title, body=body, parent=parent, owner=_actor(owner),
            tags=list(tags or []), priority=priority,
            families=list(families or []), external_refs=dict(external_refs or {}),
            **kw))

    def patch(self, ticket_id: str, *, actor: Any = None, status: Any = None,
              **fields) -> dict:
        st = coerce_status(status) if status is not None else None
        return _to_dict(self._store.update_ticket(
            ticket_id, status=st, actor=_actor(actor), **fields))

    def append_event(self, ticket_id: str, kind: str, actor: Any = None,
                     payload: Optional[dict] = None) -> dict:
        return _to_dict(self._store.append_event(
            ticket_id, kind, actor=_actor(actor), payload=dict(payload or {})))

    def add_edge(self, src: str, dst: str, type: str,
                 meta: Optional[dict] = None) -> dict:
        return _to_dict(self._store.add_edge(src, dst, type, meta=dict(meta or {})))

    def bind_thread(self, ticket_id: str, *, platform: str, room_id: str,
                    thread_id: Optional[str] = None,
                    root_msg_id: Optional[str] = None) -> dict:
        return _to_dict(self._store.bind_thread(
            ticket_id, platform=platform, room_id=room_id,
            thread_id=thread_id, root_msg_id=root_msg_id))

    # -- reads, dict-ified --------------------------------------------------

    def get(self, ticket_id: str) -> Optional[dict]:
        return _to_dict(self._store.get_ticket(ticket_id))

    def list(self, *, status: Optional[str] = None, family: Optional[str] = None,
             tag: Optional[str] = None, q: Optional[str] = None) -> list[dict]:
        return [_to_dict(t) for t in self._store.list_tickets(
            status=status, family=family, tag=tag, q=q)]

    def children(self, ticket_id: str) -> list[dict]:
        return [_to_dict(t) for t in self._store.children(ticket_id)]

    def ancestry(self, ticket_id: str) -> list[dict]:
        return [_to_dict(t) for t in self._store.ancestry(ticket_id)]

    def descendants(self, ticket_id: str) -> list[dict]:
        return [_to_dict(t) for t in self._store.descendants(ticket_id)]

    def related(self, ticket_id: str) -> list[dict]:
        return [_to_dict(t) for t in self._store.related(ticket_id)]

    def events(self, ticket_id: str) -> list[dict]:
        return [_to_dict(e) for e in self._store.events(ticket_id)]

    def timeline(self, ticket_id: str) -> list[dict]:
        return self._store.timeline(ticket_id)

    def board(self) -> dict[str, list[dict]]:
        return {k: [_to_dict(t) for t in v]
                for k, v in self._store.board().items()}

    def list_bindings(self, ticket_id: str) -> list[dict]:
        return [_to_dict(b) for b in self._store.bindings(ticket_id)]

    def get_binding(self, *, platform: str, room_id: str,
                    thread_id: Optional[str] = None) -> Optional[dict]:
        conn = self._store._conn
        row = conn.execute(
            "SELECT id, ticket_id, platform, room_id, thread_id, root_msg_id, created_at "
            "FROM thread_bindings WHERE platform = ? AND room_id = ? "
            "AND IFNULL(thread_id, '') = IFNULL(?, '') "
            "ORDER BY created_at, rowid LIMIT 1",
            (platform, room_id, thread_id),
        ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self._store.close()
