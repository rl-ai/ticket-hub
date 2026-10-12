"""hub_cli — terminal front-end for the ticket-hub API.

Human output is one compact line per ticket: ``ID  STATUS  TITLE  [owner]``.
Every read verb takes ``--json`` for machine use.
Exit codes: 0 ok, 1 on HubError (message on stderr).

Usage examples:
    hub new "Fix flaky replay" --body "see logs" --tag qa --owner hermes
    hub ls --status open --json
    hub start TK-...        # draft -> open -> running (two patches if needed)
    hub done TK-...         # walks to review then done
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from hub_client import HubClient, HubError

STATUS_ORDER = ["draft", "open", "running", "blocked", "review", "done",
                "cancelled"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def ticket_line(t: dict) -> str:
    owner = t.get("owner") or {}
    name = owner.get("name")
    line = f"{t['id']}  {t.get('status', '?')}  {t.get('title', '')}"
    if name and name != "unknown":
        line += f"  [{name}]"
    return line


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _cmd_new(hub: HubClient, args) -> int:
    t = hub.create_ticket(args.title, body=args.body or "",
                          parent=args.parent, owner=args.owner,
                          tags=args.tag or None, priority=args.priority)
    print(_dump(t) if getattr(args, "json", False) else ticket_line(t))
    return 0


def _cmd_get(hub: HubClient, args) -> int:
    t = hub.get(args.id)
    print(_dump(t) if args.json else ticket_line(t))
    return 0


def _cmd_ls(hub: HubClient, args) -> int:
    tickets = hub.list(status=args.status, family=args.family,
                       tag=args.tag, q=args.q)
    if args.json:
        print(_dump(tickets))
    else:
        for t in tickets:
            print(ticket_line(t))
    return 0


def _cmd_set(hub: HubClient, args) -> int:
    if not any(v is not None for v in
               (args.status, args.priority, args.title, args.body)):
        print("set: give at least one of --status/--priority/--title/--body",
              file=sys.stderr)
        return 1
    t = hub.patch(args.id, status=args.status, priority=args.priority,
                  title=args.title, body=args.body, actor=args.actor)
    print(ticket_line(t))
    return 0


def _cmd_comment(hub: HubClient, args) -> int:
    ev = hub.comment(args.id, args.text, actor=args.actor)
    print(f"{ev['id']}  comment  on {ev['ticket_id']}")
    return 0


def _cmd_event(hub: HubClient, args) -> int:
    payload: dict = {}
    if args.json_payload is not None:
        try:
            payload = json.loads(args.json_payload)
        except ValueError as exc:
            print(f"event: --json is not valid JSON: {exc}", file=sys.stderr)
            return 1
    ev = hub.event(args.id, args.kind, payload, actor=args.actor)
    print(f"{ev['id']}  {ev['kind']}  on {ev['ticket_id']}")
    return 0


def _cmd_link(hub: HubClient, args) -> int:
    edge = hub.edge(args.id, args.dst, args.type)
    print(f"{edge['id']}  {edge['src']}  -{edge['type']}->  {edge['dst']}")
    return 0


def _cmd_plan(hub: HubClient, args) -> int:
    plan = hub.plan(args.id)
    if args.json:
        print(_dump(plan))
        return 0
    t = plan["ticket"]
    p = plan["progress"]
    print(f"{t['id']}  [{t['status']}]  {t['title']}")
    if plan.get("summary"):
        print()
        for line in plan["summary"].splitlines()[:6]:
            print(f"  {line}")
    print()
    bar_w = 30
    filled = int(bar_w * p["percent"] / 100)
    print(f"  progress  [{'#' * filled}{'.' * (bar_w - filled)}] "
          f"{p['percent']}%  ({p['done']}/{p['total']} done)")
    if plan["blocked_by_ray"]:
        print(f"\n  BLOCKED BY RAY ({len(plan['blocked_by_ray'])}):")
        for b in plan["blocked_by_ray"][:10]:
            print(f"    ! {b['id']}  [{b['status']}]  {b['title']}")
    if plan["remaining_items"]:
        print(f"\n  REMAINING ({len(plan['remaining_items'])}):")
        for r in plan["remaining_items"][:12]:
            owner = f"  [{r['owner']}]" if r.get("owner") else ""
            print(f"    - {r['id']}  [{r['status']}]  {r['title']}{owner}")
        if len(plan["remaining_items"]) > 12:
            print(f"    … {len(plan['remaining_items']) - 12} more")
    if plan["remaining_notes"]:
        print("\n  REMAINING (noted in plan):")
        for item in plan["remaining_notes"][:8]:
            print(f"    · {item}")
    act = plan.get("recent_activity") or []
    if act:
        print("\n  RECENT:")
        for e in act[-5:]:
            actor = (e.get("actor") or {}).get("name", "?")
            print(f"    {e.get('ts', '')[:19]}  {actor}: {e.get('kind')}")
    return 0


def _cmd_lineage(hub: HubClient, args) -> int:
    lin = hub.lineage(args.id)
    if args.json:
        print(_dump(lin))
        return 0
    ancestry = lin.get("ancestry", [])
    if ancestry:
        chain = " -> ".join(t["id"] for t in ancestry)
        print(f"ancestry:  {chain} -> {lin['ticket']['id']}")
    print(ticket_line(lin["ticket"]))
    print(f"children: {len(lin.get('descendants', []))}  "
          f"related: {len(lin.get('related', []))}  "
          f"events: {len(lin.get('events', []))}")
    return 0


def _cmd_digest(hub: HubClient, args) -> int:
    if args.md:
        text = hub.digest(args.id, fmt="md", full=args.full)
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0
    digest = hub.digest(args.id, fmt="json", full=args.full)
    if args.json:
        print(_dump(digest))
        return 0
    print(f"# Updates — {digest.get('project_title') or digest.get('project')}")
    for b in digest.get("bullets", []):
        cites = ", ".join(b.get("event_ids", []))
        print(f"- {b['text']} ({cites})")
    return 0


def _cmd_board(hub: HubClient, args) -> int:
    board = hub.board()
    if args.json:
        print(_dump(board))
        return 0
    columns = board.get("columns", {})
    counts = board.get("child_counts", {})
    order = STATUS_ORDER + [c for c in columns if c not in STATUS_ORDER]
    for status in order:
        tickets = columns.get(status, [])
        if not tickets:
            continue
        print(f"== {status} ({len(tickets)}) ==")
        for t in tickets:
            line = ticket_line(t)
            kids = counts.get(t["id"], 0)
            if kids:
                line += f"  ({kids} children)"
            print(line)
    return 0


def _cmd_bind(hub: HubClient, args) -> int:
    b = hub.bind(args.id, args.platform, args.room,
                 thread_id=args.thread, root_msg_id=args.root)
    print(f"{b.get('id', '')}  bound  {b['platform']}:{b['room_id']}"
          + (f"  thread={b['thread_id']}" if b.get("thread_id") else ""))
    return 0


def _walk_to(hub: HubClient, ticket_id: str, target: str,
             actor: str | None) -> dict:
    """Multi-hop status walk using only legal transitions."""
    t = hub.get(ticket_id)
    if target == "running":
        if t["status"] == "draft":
            t = hub.patch(ticket_id, status="open", actor=actor)
        if t["status"] != "running":
            t = hub.patch(ticket_id, status="running", actor=actor)
    elif target == "done":
        if t["status"] == "draft":
            t = hub.patch(ticket_id, status="open", actor=actor)
        if t["status"] not in ("review", "done"):
            t = hub.patch(ticket_id, status="review", actor=actor)
        if t["status"] != "done":
            t = hub.patch(ticket_id, status="done", actor=actor)
    else:  # pragma: no cover - guarded by argparse choices
        raise ValueError(f"unknown target status: {target}")
    return t


def _cmd_start(hub: HubClient, args) -> int:
    print(ticket_line(_walk_to(hub, args.id, "running", args.actor)))
    return 0


def _cmd_done(hub: HubClient, args) -> int:
    print(ticket_line(_walk_to(hub, args.id, "done", args.actor)))
    return 0


# ---------------------------------------------------------------------------
# Parser / entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hub", description="ticket-hub command-line client")
    p.add_argument("--url", help="hub base URL (default: $TICKET_HUB_URL or "
                                 "http://127.0.0.1:8777)")
    p.add_argument("--timeout", type=float, default=None,
                   help="HTTP timeout seconds")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("new", help="create a ticket")
    sp.add_argument("title")
    sp.add_argument("--body", default="")
    sp.add_argument("--parent", default=None)
    sp.add_argument("--owner", default=None)
    sp.add_argument("--tag", action="append", default=[])
    sp.add_argument("--priority", default=None)
    sp.add_argument("--json", action="store_true", help="print created ticket as raw JSON")
    sp.set_defaults(func=_cmd_new)

    sp = sub.add_parser("get", help="show one ticket")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_get)

    sp = sub.add_parser("ls", help="list tickets")
    sp.add_argument("--status", default=None)
    sp.add_argument("--family", default=None)
    sp.add_argument("--tag", default=None)
    sp.add_argument("--q", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_ls)

    sp = sub.add_parser("set", help="patch fields on a ticket")
    sp.add_argument("id")
    sp.add_argument("--status", default=None)
    sp.add_argument("--priority", default=None)
    sp.add_argument("--title", default=None)
    sp.add_argument("--body", default=None)
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_set)

    sp = sub.add_parser("comment", help="add a comment event")
    sp.add_argument("id")
    sp.add_argument("text")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_comment)

    sp = sub.add_parser("event", help="append a typed event")
    sp.add_argument("id")
    sp.add_argument("kind")
    sp.add_argument("--json", dest="json_payload", default=None,
                    help="event payload as a JSON object string")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_event)

    sp = sub.add_parser("link", help="add an edge ID -> DST of TYPE")
    sp.add_argument("id")
    sp.add_argument("dst")
    sp.add_argument("type")
    sp.set_defaults(func=_cmd_link)

    sp = sub.add_parser("lineage", help="ancestry + descendants + events")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_lineage)

    sp = sub.add_parser("plan",
                        help="what this ticket is about, progress, what remains")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_plan)

    sp = sub.add_parser("digest", help="translated project digest")
    sp.add_argument("id")
    sp.add_argument("--md", action="store_true", help="markdown output")
    sp.add_argument("--json", action="store_true", help="raw JSON output")
    sp.add_argument("--full", action="store_true",
                    help="canonical lane-C translation (all event kinds)")
    sp.set_defaults(func=_cmd_digest)

    sp = sub.add_parser("board", help="tickets grouped by status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_board)

    sp = sub.add_parser("bind", help="bind a chat thread to a ticket")
    sp.add_argument("id")
    sp.add_argument("platform")
    sp.add_argument("room")
    sp.add_argument("--thread", default=None)
    sp.add_argument("--root", default=None)
    sp.set_defaults(func=_cmd_bind)

    sp = sub.add_parser("start", help="move ticket to running "
                                      "(open first if draft)")
    sp.add_argument("id")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_start)

    sp = sub.add_parser("done", help="move ticket to done "
                                     "(via open/review as needed)")
    sp.add_argument("id")
    sp.add_argument("--actor", default=None)
    sp.set_defaults(func=_cmd_done)

    return p


def main(argv: list[str] | None = None,
         client: HubClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    hub = client or HubClient(base_url=args.url,
                              **({} if args.timeout is None
                                 else {"timeout": args.timeout}))
    try:
        return args.func(hub, args)
    except HubError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
