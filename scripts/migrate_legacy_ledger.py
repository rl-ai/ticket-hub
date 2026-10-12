#!/usr/bin/env python3
"""One-off: migrate the legacy flat ticket ledger into ticket-hub.

Legacy: ~/tp-data/tickets/tickets.db (tickets/ticket_events/ticket_edges).
Hub:    TICKET_HUB_DB or ~/tp-data/ticket-hub/hub.db.

Idempotent: tickets are keyed by external_refs.legacy_id; events by
payload.legacy_event_id; edges by (legacy src/dst/kind). Original ts and
actor names are preserved via direct inserts (append-only triggers only
block UPDATE/DELETE, inserts are legal).

Legacy state -> hub status: open/claimed->running (claimed) / open (open),
done->done, cancelled->cancelled, blocked->blocked, review->review.
Legacy 'child_of' edge (src child -> dst parent) -> hub parent_of edge
(hub direction: src=parent, dst=child; ancestry SQL: edges.src JOIN ...
WHERE e.dst = ticket AND type='parent_of').
"""
import json
import os
import sqlite3
import sys

LEGACY = os.path.expanduser("~/tp-data/tickets/tickets.db")
HUB = os.environ.get("TICKET_HUB_DB", os.path.expanduser("~/tp-data/ticket-hub/hub.db"))

STATE_MAP = {
    "open": "open", "claimed": "running", "running": "running",
    "blocked": "blocked", "review": "review", "done": "done",
    "cancelled": "cancelled",
}

KIND_MAP = {  # legacy kind -> hub EVENT_KIND; unmapped -> comment
    "note": "comment", "transition": "status_changed", "claim": "checkpoint",
    "artifact": "artifact", "approval-receipt": "approval_receipt",
    "review-verdict": "approval_receipt", "plan": "checkpoint",
    "test-evidence": "checkpoint", "changelog-line": "comment",
}


def main():
    dry = "--apply" not in sys.argv
    leg = sqlite3.connect(LEGACY)
    leg.row_factory = sqlite3.Row
    hub = sqlite3.connect(HUB, check_same_thread=False)
    hub.row_factory = sqlite3.Row
    hub.execute("PRAGMA foreign_keys=ON")

    existing = {}
    for r in hub.execute("SELECT id, external_refs FROM tickets"):
        try:
            lid = json.loads(r["external_refs"] or "{}").get("legacy_id")
        except Exception:
            lid = None
        if lid:
            existing[lid] = r["id"]
    existing_ev = set()
    for r in hub.execute("SELECT payload FROM events"):
        try:
            lid = json.loads(r["payload"] or "{}").get("legacy_event_id")
        except Exception:
            lid = None
        if lid:
            existing_ev.add(lid)
    existing_edge = set()
    for r in hub.execute("SELECT e.src, e.dst, e.type, e.meta FROM edges e"):
        try:
            m = json.loads(r["meta"] or "{}")
        except Exception:
            m = {}
        if m.get("legacy_edge"):
            existing_edge.add(m["legacy_edge"])

    from ticket_hub.model import new_id

    def actor_json(name):
        name = name or "unknown"
        fam = "ray" if name == "ray" else ("system" if name == "system" else "legacy")
        kind = "human" if name == "ray" else ("system" if name == "system" else "agent")
        return {"id": name, "name": name, "family": fam, "kind": kind}

    def ensure_actor(name):
        a = actor_json(name)
        hub.execute(
            "INSERT OR IGNORE INTO actors (id, name, family, kind) VALUES (?,?,?,?)",
            (a["id"], a["name"], a["family"], a["kind"]))

    # -- tickets ----------------------------------------------------------
    n_t = n_link = 0
    for r in leg.execute("SELECT * FROM tickets ORDER BY created_at"):
        lid = r["ticket_id"]
        if lid in existing:
            continue
        hid = new_id("TK")
        status = STATE_MAP.get(r["state"], "open")
        owner = r["created_by"] or "legacy"
        ensure_actor(owner)
        ext = json.dumps({"legacy_id": lid, "legacy_ledger": LEGACY})
        hub.execute(
            "INSERT INTO tickets (id, title, body, status, priority, owner_id, tags, families,"
            " external_refs, artifacts, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (hid, r["title"], r["body"] if "body" in r.keys() else "", status,
             "high" if (r["priority"] or 0) >= 3 else "normal", owner,
             json.dumps(["legacy"]), json.dumps(["legacy"]), ext, "[]",
             r["created_at"], r["updated_at"]))
        existing[lid] = hid
        n_t += 1
        # ray_gated is intent we must not lose
        if r["ray_gated"]:
            hub.execute(
                "INSERT INTO events (id, ticket_id, ts, actor_id, actor_json, kind, payload)"
                " VALUES (?,?,?,?,?,?,?)",
                (new_id("EV"), hid, r["created_at"], "system",
                 json.dumps(actor_json("system")), "checkpoint",
                 json.dumps({"legacy_event_id": f"{lid}:ray_gated",
                             "marker": "RAY-GATED", "note":
                             "legacy ledger flagged this ticket ray-gated"})))
    # -- edges -------------------------------------------------------------
    for r in leg.execute("SELECT * FROM ticket_edges"):
        key = f"{r['src_ticket']}|{r['dst_ticket']}|{r['kind']}"
        if key in existing_edge:
            continue
        s, d = existing.get(r["src_ticket"]), existing.get(r["dst_ticket"])
        if not s or not d:
            continue
        if r["kind"] == "child_of":
            typ, src, dst = "parent_of", d, s      # legacy src=child,dst=parent -> hub parent->child
        elif r["kind"] in ("parent_of",):
            typ, src, dst = "parent_of", s, d
        else:
            typ, src, dst = "relates_to", s, d
        try:
            hub.execute(
                "INSERT INTO edges (id, src, dst, type, meta, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (new_id("ED"), src, dst, typ,
                 json.dumps({"legacy_edge": key, "legacy_kind": r["kind"]}),
                 r["created_at"]))
            n_link += 1
        except sqlite3.IntegrityError:
            pass  # single-parent rule: skip conflicting second parent
    # -- events ------------------------------------------------------------
    n_e = 0
    last_status = {row["id"]: row["status"] for row in hub.execute("SELECT id, status FROM tickets")}
    for r in leg.execute("SELECT * FROM ticket_events ORDER BY ticket_id, ts, seq"):
        lid = r["ticket_id"]
        hid = existing.get(lid) if lid else None
        if not hid or r["event_id"] in existing_ev:
            continue
        kind = KIND_MAP.get(r["kind"], "comment")
        try:
            body = json.loads(r["body_json"] or "{}")
        except Exception:
            body = {"msg": r["body_json"]}
        payload = {"legacy_event_id": r["event_id"], "legacy_kind": r["kind"],
                   **body}
        actor = r["actor"] or "legacy"
        ensure_actor(actor)
        if kind == "status_changed":
            to = STATE_MAP.get(body.get("to"), "open")
            payload["to"] = to
            payload["from"] = last_status.get(hid, "open")
            last_status[hid] = to
        if r["artifact_sha256"]:
            payload["sha256"] = r["artifact_sha256"]
        if r["failure_domain"] or r["failure_class"]:
            payload["failure"] = {"domain": r["failure_domain"], "class": r["failure_class"]}
        hub.execute(
            "INSERT INTO events (id, ticket_id, ts, actor_id, actor_json, kind, payload)"
            " VALUES (?,?,?,?,?,?,?)",
            (new_id("EV"), hid, r["ts"], actor, json.dumps(actor_json(actor)), kind,
             json.dumps(payload, default=str)))
        n_e += 1
    if dry:
        hub.rollback()
        print(f"DRY tickets+{n_t} edges+{n_link} events+{n_e} (rolled back)")
    else:
        hub.commit()
        print(f"APPLIED tickets+{n_t} edges+{n_link} events+{n_e}")
    hub.close()


if __name__ == "__main__":
    main()
