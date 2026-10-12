"""ticket-hub HTTP API (Lane B).

Serves the DESIGN.md HTTP contract on top of any Store object matching the
ticket_hub.store.py signatures (create_ticket, get, list, patch, append_event,
add_edge, children, ancestry, descendants, related, timeline, bind_thread,
board). Lane A's real store is resolved lazily; tests inject a FakeStore.

Run:
    python api.py            # port from $TICKET_HUB_PORT, default 8777
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(APP_DIR, "ui")

STATUSES = [s.value for s in __import__("ticket_hub.model", fromlist=["Status"]).Status]


def _canon_status(value: str) -> str:
    """Case-insensitive status normalization to the canonical lowercase value."""
    v = str(value).strip().lower()
    if v not in STATUSES:
        raise ValueError(f"Unknown status: {value}")
    return v
EDGE_TYPES = {"parent_of", "blocks", "relates_to", "duplicate_of"}
# Types that must stay acyclic in the graph.
CYCLE_EDGE_TYPES = {"parent_of", "blocks"}
EVENT_KINDS = {
    "created", "updated", "status_changed", "comment", "checkpoint",
    "decision", "approval_receipt", "artifact", "handoff",
    "thread_message", "link",
}
PLATFORMS = {"matrix", "discord", "slack", "fake"}
KNOWN_ISSUE_MARKERS = ("QUEUED FINDING", "KNOWN-FAILS")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def normalize_actor(raw: Any) -> dict:
    """Coerce an owner/actor (string, dict, or None) into the Actor shape."""
    if raw is None:
        return {"id": "unknown", "name": "unknown", "family": "unknown", "kind": "agent"}
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        raise ValueError("actor must be a string or object")
    name = raw.get("name") or raw.get("id") or "unknown"
    return {
        "id": str(raw.get("id") or name),
        "name": str(name),
        "family": str(raw.get("family") or "unknown"),
        "kind": str(raw.get("kind") or "agent"),
    }


def _is_known_issue(payload: dict) -> bool:
    for value in (payload or {}).values():
        if isinstance(value, str):
            upper = value.upper()
            if any(marker in upper for marker in KNOWN_ISSUE_MARKERS):
                return True
    return False


def translate_digest(project: dict, events: list[dict]) -> list[dict]:
    """Template pass per DESIGN.md translation rules.

    Maps event kinds to reader-facing sentences; never fabricates — every
    bullet cites the event ids it paraphrases.
    """
    bullets: list[dict] = []
    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        text = None
        if _is_known_issue(payload):
            text = "known issue logged"
        elif kind == "status_changed":
            text = f"advanced to {str(payload.get('to', '?')).capitalize()}"
        elif kind == "artifact":
            name = payload.get("name") or payload.get("artifact") or "an artifact"
            text = f"produced {name}"
        elif kind == "decision":
            text = "decision recorded"
        elif kind == "approval_receipt":
            approver = payload.get("approver") or (ev.get("actor") or {}).get("name")
            text = f"approved by {approver}" if approver else "approval recorded"
        if text:
            bullets.append({"text": text, "event_ids": [ev.get("id")]})
    return bullets


def digest_markdown(digest: dict) -> str:
    lines = [f"# Updates — {digest.get('project_title') or digest.get('project')}"]
    for bullet in digest.get("bullets", []):
        cites = ", ".join(bullet.get("event_ids", []))
        lines.append(f"- {bullet['text']} ({cites})")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class TicketCreate(BaseModel):
    title: str
    body: str = ""
    parent: Optional[str] = None
    owner: Optional[Any] = None
    tags: list[str] = []
    families: list[str] = []
    priority: str = "medium"
    external_refs: dict[str, Any] = {}


class TicketPatch(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    actor: Optional[Any] = None


class EventCreate(BaseModel):
    kind: str
    actor: Optional[Any] = None
    payload: dict[str, Any] = {}


class EdgeCreate(BaseModel):
    type: str
    dst: str
    meta: dict[str, Any] = {}


class BindCreate(BaseModel):
    platform: str
    room_id: str
    thread_id: Optional[str] = None
    root_msg_id: Optional[str] = None


class InboundMessage(BaseModel):
    room_id: str
    thread_id: Optional[str] = None
    root_msg_id: Optional[str] = None
    actor: Optional[Any] = None
    text: str


class WikiPageCreate(BaseModel):
    title: str
    body_md: str = ""
    tags: list[str] = []
    actor: Optional[Any] = None


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def create_app(store: Any = None) -> FastAPI:
    app = FastAPI(title="ticket-hub", version="0.1.0")
    app.state.store = store

    def get_store():
        if app.state.store is None:
            try:
                import os
                from ticket_hub.store import Store  # Lane A
                from hub_gateway import StoreGateway
            except Exception as exc:  # pragma: no cover - depends on env
                raise HTTPException(
                    status_code=503,
                    detail={"code": "store_unavailable",
                            "message": f"No store configured: {exc}"},
                )
            db_path = os.environ.get(
                "TICKET_HUB_DB",
                os.path.expanduser("~/tp-data/ticket-hub/hub.db"),
            )
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            app.state.store = StoreGateway(Store(db_path))
        return app.state.store

    def get_ticket_or_404(ticket_id: str) -> dict:
        ticket = get_store().get(ticket_id)
        if ticket is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Ticket {ticket_id} not found"},
            )
        return ticket

    # ---- JSON error envelope ---------------------------------------------

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "code": "error", "message": str(exc.detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request, exc):
        return JSONResponse(
            {"error": {"code": "validation",
                       "message": "Request validation failed",
                       "details": exc.errors()}},
            status_code=422,
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(request, exc):
        return JSONResponse(
            {"error": {"code": "not_found", "message": str(exc).strip("'")}},
            status_code=404,
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc):
        return JSONResponse(
            {"error": {"code": "bad_request", "message": str(exc)}},
            status_code=400,
        )

    # ---- Tickets ----------------------------------------------------------

    @app.post("/tickets", status_code=201)
    def create_ticket(req: TicketCreate):
        owner = normalize_actor(req.owner)
        families = list(req.families)
        if not families and owner.get("family") and owner["family"] != "unknown":
            families = [owner["family"]]
        return get_store().create_ticket(
            title=req.title,
            body=req.body,
            parent=req.parent,
            owner=owner,
            tags=list(req.tags),
            families=families,
            priority=req.priority,
            external_refs=dict(req.external_refs),
        )

    @app.get("/tickets")
    def list_tickets(status: Optional[str] = None, family: Optional[str] = None,
                     tag: Optional[str] = None, q: Optional[str] = None):
        if status:
            status = _canon_status(status)
        tickets = get_store().list(status=status, family=family, tag=tag, q=q)
        return list(tickets)

    @app.get("/tickets/{ticket_id}")
    def get_ticket(ticket_id: str):
        return get_ticket_or_404(ticket_id)

    @app.patch("/tickets/{ticket_id}")
    def patch_ticket(ticket_id: str, req: TicketPatch):
        if req.status is not None:
            req.status = _canon_status(req.status)
        fields = {k: v for k, v in
                  (("status", req.status), ("priority", req.priority),
                   ("title", req.title), ("body", req.body))
                  if v is not None}
        if not fields:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": "No fields to update"})
        actor = normalize_actor(req.actor)
        return get_store().patch(ticket_id, actor=actor, **fields)

    @app.post("/tickets/{ticket_id}/events", status_code=201)
    def append_event(ticket_id: str, req: EventCreate):
        get_ticket_or_404(ticket_id)
        if req.kind not in EVENT_KINDS:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown event kind: {req.kind}"})
        actor = normalize_actor(req.actor)
        return get_store().append_event(ticket_id, kind=req.kind, actor=actor,
                                        payload=dict(req.payload))

    @app.post("/tickets/{ticket_id}/edges", status_code=201)
    def add_edge(ticket_id: str, req: EdgeCreate):
        get_ticket_or_404(ticket_id)
        if req.type not in EDGE_TYPES:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown edge type: {req.type}"})
        get_ticket_or_404(req.dst)
        # API-level cycle guard (the store also guards; defense in depth).
        if req.type in CYCLE_EDGE_TYPES:
            store = get_store()
            if req.dst == ticket_id or any(
                d.get("id") == ticket_id for d in store.descendants(req.dst)
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "cycle_rejected",
                            "message": "Edge would create a cycle"},
                )
        return get_store().add_edge(ticket_id, req.dst, req.type, dict(req.meta))

    # ---- Projections ------------------------------------------------------

    def _build_tree(ticket_id: str, depth: int = 0, seen: Optional[set] = None) -> dict:
        seen = seen if seen is not None else set()
        store = get_store()
        node = {"ticket": get_ticket_or_404(ticket_id), "children": []}
        if ticket_id in seen or depth > 64:
            return node
        seen.add(ticket_id)
        for child in store.children(ticket_id):
            node["children"].append(_build_tree(child["id"], depth + 1, seen))
        return node

    @app.get("/tickets/{ticket_id}/lineage")
    def lineage(ticket_id: str):
        get_ticket_or_404(ticket_id)
        store = get_store()
        descendants = list(store.descendants(ticket_id))
        event_ids = {ticket_id} | {d["id"] for d in descendants}
        events: list[dict] = []
        for tid in event_ids:
            events.extend(store.timeline(tid))
        events.sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
        return {
            "ticket": get_ticket_or_404(ticket_id),
            "ancestry": list(store.ancestry(ticket_id)),
            "descendants": descendants,
            "related": list(store.related(ticket_id)),
            "tree": _build_tree(ticket_id),
            "events": events,
        }

    @app.get("/tickets/{ticket_id}/digest")
    def digest(ticket_id: str, format: str = "json", full: bool = False):
        if format not in ("json", "md"):
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": "format must be json|md"})
        ticket = get_ticket_or_404(ticket_id)
        store = get_store()
        ancestry = list(store.ancestry(ticket_id))
        project = ancestry[0] if ancestry else ticket
        descendants = list(store.descendants(ticket_id))
        ids = {ticket_id} | {d["id"] for d in descendants}
        events: list[dict] = []
        for tid in ids:
            events.extend(store.timeline(tid))
        events.sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
        if full:
            # Canonical lane-C translation layer (all event kinds, no
            # fabrication, cites event ids).
            from ticket_hub import translate as _translate
            canon = _translate.template_pass(events=events, project=project)
            bullets = canon.get("bullets", [])
        else:
            bullets = translate_digest(project, events)
        payload = {
            "project": project["id"],
            "project_title": project.get("title"),
            "ticket": ticket["id"],
            "updated_at": ticket.get("updated_at"),
            "bullets": bullets,
        }
        if format == "md":
            return Response(digest_markdown(payload), media_type="text/markdown")
        return payload

    @app.get("/board")
    def board():
        store = get_store()
        columns = store.board()
        child_counts = {}
        for status_tickets in columns.values():
            for t in status_tickets:
                child_counts[t["id"]] = len(list(store.children(t["id"])))
        return {"columns": columns, "child_counts": child_counts}

    @app.get("/health")
    def health():
        """Last-known health written by scripts/healthcheck.sh (monitor
        surface per GLM review: a FAIL must be visible between checks)."""
        path = os.environ.get(
            "TICKET_HUB_HEALTH_STATUS",
            os.path.expanduser("~/tp-data/ticket-hub/health-status.json"))
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"ok": True, "ts": None, "last_error": None,
                    "note": "no healthcheck has run yet"}

    def _read_json_file(path: str) -> dict | None:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @app.get("/admin/status")
    def admin_status():
        """Mission Control aggregate. Read-only: local files + launchctl
        list (display-only; actions stay blocked by Ray)."""
        hub_dir = os.environ.get(
            "TICKET_HUB_DIR", os.path.expanduser("~/tp-data/ticket-hub"))
        store = get_store()

        counts = {}
        try:
            conn = getattr(store, "_store", store)
            raw = getattr(conn, "_conn", None)
            if raw is not None:
                for table, key in (("tickets", "tickets"), ("edges", "edges"),
                                   ("events", "events"),
                                   ("wiki_pages", "wiki_pages")):
                    try:
                        counts[key] = raw.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except Exception:
                        counts[key] = None
        except Exception:
            pass

        services = []
        try:
            import subprocess
            out = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True,
                timeout=5).stdout
            for line in out.splitlines():
                cols = line.split("\t")
                if len(cols) >= 3 and cols[2].startswith("com.ticket-hub."):
                    pid, status, label = cols[0], cols[1], cols[2]
                    services.append({
                        "label": label,
                        "loaded": True,
                        "pid": None if pid in ("-", "") else int(pid),
                        "state": ("running" if pid not in ("-", "")
                                  else "loaded (not running)")})
        except Exception:
            pass

        backups = {"newest": None, "age_h": None, "count": 0}
        try:
            bdir = os.path.join(hub_dir, "backups")
            snaps = sorted(
                (f for f in os.listdir(bdir) if f.endswith(".db")),
                key=lambda f: os.path.getmtime(os.path.join(bdir, f)),
                reverse=True)
            if snaps:
                import time
                backups = {"newest": snaps[0],
                           "age_h": round(
                               (time.time() - os.path.getmtime(
                                   os.path.join(bdir, snaps[0]))) / 3600, 1),
                           "count": len(snaps)}
        except OSError:
            pass

        return {
            "health": _read_json_file(
                os.path.join(hub_dir, "health-status.json"))
            or {"ok": None, "ts": None, "tickets": None,
                "last_error": "no healthcheck has run"},
            "services": services,
            "backups": backups,
            "maintenance": _read_json_file(
                os.path.join(hub_dir, "maintenance-status.json"))
            or {"last_run": None, "summary": "not run yet"},
            "hygiene": _read_json_file(
                os.path.join(hub_dir, "hygiene-status.json"))
            or {"result": "unknown", "ts": None},
            "counts": counts,
            "db_path": hub_dir + "/hub.db",
            "seats_note": "seat health: run remote-model-health (morpheus); "
                          "not polled from the hub by design (loopback scope)",
        }

    @app.get("/tickets/{ticket_id}/plan")
    def plan(ticket_id: str):
        """Introspection view: what this ticket is about, progress, and
        what remains. Progress = descendant work items done vs open;
        remaining = unfinished children + latest 'remaining items' checkpoint
        (checkpoint events may carry payload.remaining: [..]); blocked_by_ray
        = open items flagged blocked/RAY-GATED (marker event or blocked
        status)."""
        store = get_store()
        ticket = get_ticket_or_404(ticket_id)
        descendants = list(store.descendants(ticket_id))

        def flag_blocked(t) -> bool:
            # Current state only: completed/cancelled items drop off even if
            # they carry historical RAY-GATED marker events (GLM review).
            if t["status"] in ("done", "cancelled"):
                return False
            if t["status"] == "blocked":
                return True
            return any(
                (e.get("payload") or {}).get("marker") == "RAY-GATED"
                for e in store.timeline(t["id"]))

        done = [t for t in descendants if t["status"] == "done"]
        cancelled = [t for t in descendants if t["status"] == "cancelled"]
        open_items = [t for t in descendants
                      if t["status"] not in ("done", "cancelled")]
        blocked = [t for t in descendants if flag_blocked(t)]
        # latest checkpoint listing remaining items (hand-authored plans)
        remaining_notes: list[dict] = []
        for tid in {ticket_id} | {t["id"] for t in descendants}:
            for ev in store.timeline(tid):
                rem = (ev.get("payload") or {}).get("remaining")
                if isinstance(rem, list) and rem:
                    remaining_notes.append(
                        {"ticket_id": tid, "ts": ev.get("ts", ""),
                         "items": [str(x) for x in rem]})
        remaining_notes.sort(key=lambda n: n["ts"])
        total_work = len(descendants) - len(cancelled)
        pct = int(100 * len(done) / total_work) if total_work else (
            100 if ticket["status"] == "done" else 0)
        brief = {"ticket": ticket,
                 "summary": (ticket.get("body") or "").strip(),
                 "progress": {"done": len(done), "open": len(open_items),
                              "total": total_work, "percent": pct},
                 "remaining_items": [
                     {"id": t["id"], "title": t["title"], "status": t["status"],
                      "owner": (t.get("owner") or {}).get("name")}
                     for t in open_items],
                 "remaining_notes": remaining_notes[-1]["items"] if remaining_notes else [],
                 "blocked_by_ray": [
                     {"id": t["id"], "title": t["title"], "status": t["status"]}
                     for t in blocked],
                 "recent_activity": list(store.timeline(ticket_id))[-8:]}
        return brief

    # ---- Threads / chat bridges ------------------------------------------

    @app.post("/tickets/{ticket_id}/bind", status_code=201)
    def bind_thread(ticket_id: str, req: BindCreate):
        get_ticket_or_404(ticket_id)
        if req.platform not in PLATFORMS:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown platform: {req.platform}"})
        return get_store().bind_thread(
            ticket_id, platform=req.platform, room_id=req.room_id,
            thread_id=req.thread_id, root_msg_id=req.root_msg_id,
        )

    @app.get("/tickets/{ticket_id}/bindings")
    def list_bindings(ticket_id: str):
        get_ticket_or_404(ticket_id)
        store = get_store()
        if not hasattr(store, "list_bindings"):
            raise HTTPException(status_code=501,
                                detail={"code": "unsupported",
                                        "message": "Store does not expose bindings"})
        return list(store.list_bindings(ticket_id))

    @app.post("/adapters/{platform}/inbound")
    def adapter_inbound(platform: str, req: InboundMessage):
        if platform not in PLATFORMS:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request",
                                        "message": f"Unknown platform: {platform}"})
        store = get_store()
        if not hasattr(store, "get_binding"):
            raise HTTPException(status_code=501,
                                detail={"code": "unsupported",
                                        "message": "Store does not expose bindings"})
        binding = store.get_binding(platform=platform, room_id=req.room_id,
                                    thread_id=req.thread_id)
        if not binding:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found",
                        "message": f"No thread binding for {platform} room {req.room_id}"},
            )
        actor = normalize_actor(req.actor)
        event = store.append_event(
            binding["ticket_id"],
            kind="thread_message",
            actor=actor,
            payload={
                "platform": platform,
                "room_id": req.room_id,
                "thread_id": req.thread_id,
                "root_msg_id": req.root_msg_id,
                "text": req.text,
                "chat_actor": actor.get("name"),
            },
        )
        # Reply-posts-back is Lane C (adapters/) territory; inert in v0.
        return {"event": event, "posted": False}

    # ---- Wiki (DESIGN-v2 pillar 1) ------------------------------------------

    def _wiki_or_404(slug: str) -> dict:
        page = get_store().get_page(slug)
        if page is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found",
                        "message": f"Wiki page {slug} not found"},
            )
        return page

    @app.get("/wiki")
    def list_wiki():
        """Index: latest revision per slug (slim, no body)."""
        pages = get_store().list_pages()
        return [
            {"slug": p.get("slug"), "title": p.get("title"),
             "tags": p.get("tags", []), "rev": p.get("rev"),
             "ts": p.get("ts")}
            for p in pages
        ]

    @app.get("/wiki-search")
    def search_wiki(q: str = ""):
        """Case-insensitive substring match on title/body/tags of the
        latest revision; returns slug/title/snippet."""
        store = get_store()
        if not hasattr(store, "search_pages"):
            raise HTTPException(status_code=501,
                                detail={"code": "unsupported",
                                        "message": "Store does not expose wiki"})
        results = []
        for p in store.search_pages(q):
            body = p.get("body_md") or ""
            results.append({"slug": p.get("slug"),
                            "title": p.get("title"),
                            "snippet": body[:200]})
        return results

    @app.get("/wiki/{slug}")
    def get_wiki(slug: str):
        """Full latest page + slim history (all revisions, no bodies)."""
        page = _wiki_or_404(slug)
        history = [
            {"slug": h.get("slug"), "rev": h.get("rev"),
             "title": h.get("title"), "tags": h.get("tags", []),
             "actor": h.get("actor", ""), "ts": h.get("ts")}
            for h in get_store().page_history(slug)
        ]
        return {"page": page, "history": history}

    @app.post("/wiki/{slug}", status_code=201)
    def put_wiki(slug: str, req: WikiPageCreate):
        return get_store().put_page(
            slug, title=req.title, body_md=req.body_md,
            tags=list(req.tags), actor=req.actor)

    # ---- UI ----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(UI_DIR, "index.html"))

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TICKET_HUB_PORT", "8777"))
    # Loopback-only posture (SECURITY.md); override only if you know why.
    host = os.environ.get("TICKET_HUB_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
