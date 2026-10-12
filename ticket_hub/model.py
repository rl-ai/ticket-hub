"""ticket_hub object model.

Dataclasses and enums for the ticket-hub graph: Actor, Ticket, Edge, Event,
ThreadBinding, Status. Stdlib only. Python 3.12+.

IDs are prefixed ULIDs: TK- (ticket), EV- (event), ED- (edge), TB- (thread
binding), A- (actor). See DESIGN.md for the contract.
"""
from __future__ import annotations

import enum
import os
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ID generation (pure-stdlib Crockford-base32 ULID: 48-bit ms timestamp +
# 80-bit randomness, 26 chars, lexicographically sortable).
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Return a new 26-char Crockford-base32 ULID."""
    ts = int(time.time() * 1000)
    val = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(val >> (5 * (25 - i))) & 31] for i in range(26))


def new_id(prefix: str) -> str:
    """Return a new prefixed id, e.g. ``new_id("TK") -> 'TK-01H...'``."""
    return f"{prefix}-{new_ulid()}"


# ---------------------------------------------------------------------------
# Status + transitions
# ---------------------------------------------------------------------------


class Status(str, enum.Enum):
    """Ticket lifecycle states (str-valued for JSON friendliness)."""

    DRAFT = "draft"
    OPEN = "open"
    RUNNING = "running"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    CANCELLED = "cancelled"


#: Draft -> Open -> Running -> Blocked -> Review -> Done, Cancelled.
#: Done and Cancelled are terminal in v0.
ALLOWED_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.DRAFT: frozenset({Status.OPEN, Status.CANCELLED}),
    Status.OPEN: frozenset({Status.RUNNING, Status.BLOCKED, Status.REVIEW, Status.CANCELLED}),
    Status.RUNNING: frozenset({Status.BLOCKED, Status.REVIEW, Status.CANCELLED}),
    Status.BLOCKED: frozenset({Status.RUNNING, Status.REVIEW, Status.CANCELLED}),
    Status.REVIEW: frozenset({Status.DONE, Status.RUNNING, Status.CANCELLED}),
    Status.DONE: frozenset(),
    Status.CANCELLED: frozenset(),
}


def coerce_status(value: "Status | str") -> Status:
    """Accept Status or its string value; raise ValueError otherwise."""
    if isinstance(value, Status):
        return value
    try:
        return Status(value)
    except ValueError:
        raise ValueError(f"unknown status: {value!r}") from None


def assert_transition(frm: Status, to: Status) -> None:
    """Raise ValueError if ``frm -> to`` is not an allowed transition."""
    if to not in ALLOWED_TRANSITIONS[frm]:
        raise ValueError(f"illegal status transition: {frm.value} -> {to.value}")


# ---------------------------------------------------------------------------
# Edge / event / platform vocabularies
# ---------------------------------------------------------------------------

EDGE_TYPES: frozenset[str] = frozenset({"parent_of", "blocks", "relates_to", "duplicate_of"})

EVENT_KINDS: frozenset[str] = frozenset(
    {
        "created",
        "updated",
        "status_changed",
        "comment",
        "checkpoint",
        "decision",
        "approval_receipt",
        "artifact",
        "handoff",
        "thread_message",
        "link",
    }
)

PLATFORMS: frozenset[str] = frozenset({"matrix", "discord", "slack"})

ACTOR_KINDS: frozenset[str] = frozenset({"agent", "human", "system"})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Actor:
    id: str
    name: str
    family: str = "unknown"  # e.g. "hermes", "forge", "ray"
    kind: str = "agent"  # agent | human | system

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind: {self.kind!r} (expected one of {sorted(ACTOR_KINDS)})")

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "family": self.family, "kind": self.kind}


SYSTEM_ACTOR = Actor(id="system", name="system", family="system", kind="system")


@dataclass
class Ticket:
    id: str
    title: str
    body: str = ""
    status: Status = Status.DRAFT
    priority: str = "normal"
    owner: Actor | None = None
    tags: list[str] = field(default_factory=list)
    families: list[str] = field(default_factory=list)
    external_refs: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "status": self.status.value,
            "priority": self.priority,
            "owner": self.owner.to_dict() if self.owner else None,
            "tags": list(self.tags),
            "families": list(self.families),
            "external_refs": dict(self.external_refs),
            "artifacts": list(self.artifacts),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Edge:
    id: str
    src: str  # ticket id
    dst: str  # ticket id
    type: str  # parent_of | blocks | relates_to | duplicate_of
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "src": self.src, "dst": self.dst, "type": self.type, "meta": dict(self.meta)}


@dataclass(frozen=True)
class Event:
    """Immutable history entry. There is deliberately no way to mutate or
    delete an event (no update/delete API; DB triggers enforce append-only)."""

    id: str
    ticket_id: str
    ts: str  # ISO-8601 UTC
    actor: Actor | None
    kind: str
    payload: dict = field(default_factory=dict)
    seq: int = 0  # store-assigned monotonic ordering key

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {self.kind!r} (expected one of {sorted(EVENT_KINDS)})")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "ts": self.ts,
            "seq": self.seq,
            "actor": self.actor.to_dict() if self.actor else None,
            "kind": self.kind,
            "payload": dict(self.payload),
        }


@dataclass
class WikiPage:
    """One immutable revision of a wiki page (DESIGN-v2 pillar 1).

    Current page for a slug = max(rev). Like events there is deliberately no
    update/delete path: revisions are append-only (DB triggers enforce it).
    ``actor`` is the plain name recorded on the write (audit, like events).
    """

    id: str  # page_id, WP-<ulid>
    slug: str
    rev: int
    title: str
    body_md: str = ""
    tags: list[str] = field(default_factory=list)
    actor: str = ""
    ts: str = ""  # ISO-8601 UTC

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "rev": self.rev,
            "title": self.title,
            "body_md": self.body_md,
            "tags": list(self.tags),
            "actor": self.actor,
            "ts": self.ts,
        }

    def slim(self) -> dict:
        """Index/history shape: no body (cheap lists)."""
        return {
            "id": self.id,
            "slug": self.slug,
            "rev": self.rev,
            "title": self.title,
            "tags": list(self.tags),
            "ts": self.ts,
        }


@dataclass
class ThreadBinding:
    ticket_id: str
    platform: str  # matrix | discord | slack
    room_id: str
    thread_id: str | None = None
    root_msg_id: str | None = None
    id: str = ""

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError(f"unknown platform: {self.platform!r} (expected one of {sorted(PLATFORMS)})")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "platform": self.platform,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "root_msg_id": self.root_msg_id,
        }
