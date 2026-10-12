# ticket-hub

The graph work OS for Ray's multi-agent operation. Every request or project
is a typed ticket in one durable store; tickets form a graph (parent/child
plans, blocks, relates_to, duplicate_of); every change is an **append-only
immutable event**; humans see it in a browser UI, agents drive it from any
terminal via the `hub` CLI or the stdlib `HubClient`, and it mirrors into the
chat threads people actually use (Matrix / Discord / Slack).

Three questions, always answerable:

1. **What is it?** — the ticket graph: a parent ticket holds the plan, its
   children are the work items, its body is the summary.
2. **How far along is it?** — `hub plan <ID>`: progress is children done vs
   total, computed from real statuses, never hand-claimed.
3. **What's left?** — the same view: remaining children, hand-noted
   remaining items, and a **Blocked by Ray** list (anything waiting on
   Ray's approval — blocked status or a `RAY-GATED` marker event).

## 60-second quickstart

```bash
# 1. Start the server (SQLite at ~/tp-data/ticket-hub/hub.db, port 8777)
cd <repo> && python api.py            # UI at http://127.0.0.1:8777/

# 2. Create the project ticket and its plan (children = work items)
hub new "Ship the exporter" --owner ray --body "Export widget data nightly"
#   -> TK-01AAA...
hub new "Write CSV writer" --parent TK-01AAA... --owner hermes
hub new "Add retry logic"  --parent TK-01AAA... --owner hermes

# 3. Work one ticket end-to-end
hub start TK-01BBB...                 # draft -> open -> running
hub comment TK-01BBB... "schema locked"
hub done TK-01BBB...                  # running -> review -> done

# 4. Ask the three questions
hub plan TK-01AAA...
#   progress  [###############.......] 50%  (1/2 done)
#   REMAINING:
#     - TK-01CCC...  [open]  Add retry logic
```

The parent + children *is* the plan. No separate plan object: `hub plan` on
the parent reads child statuses and reports percent done, what's left, and
what is Blocked by Ray.

## Status model

Canonical lowercase; transitions are enforced at the store (illegal moves
are rejected with `bad_request`):

```
            ┌──────────────────────────── cancelled
            │                ┌──────────────┐
 draft ──> open ──> running ──> review ──> done
              │         │  ▲        │
              │         ▼  │        │ (review can go back to running)
              └────> blocked ┘
```

| From | May move to |
|---|---|
| draft | open, cancelled |
| open | running, blocked, review, cancelled |
| running | blocked, review, cancelled |
| blocked | running, review, cancelled |
| review | done, running, cancelled |
| done / cancelled | — terminal |

`hub start` and `hub done` walk multi-hop paths using only legal
transitions (e.g. `done` from `draft` goes draft→open→review→done).

## Chat binding

One ticket = one chat thread root. `hub bind <ID> matrix <room> --thread
<t> --root <msg>` attaches a thread to a ticket:

- **Inbound**: a reply in the bound thread arrives at
  `POST /adapters/{platform}/inbound` and lands as an immutable
  `thread_message` event on the ticket, merged into its timeline.
- **Outbound**: `outbound.py` polls the hub, builds the digest for changed
  tickets, and posts it into bound threads — with watermark + event-id
  dedupe so nothing ever reposts. Child tickets post under the parent's
  root.

## Where to go next

| Doc | Contents |
|---|---|
| [docs/API.md](docs/API.md) | Every HTTP endpoint, request/response examples, error envelope, plan shape |
| [docs/CLI.md](docs/CLI.md) | Every `hub` verb, flags, `--json`, exit codes |
| [docs/AGENT-GUIDE.md](docs/AGENT-GUIDE.md) | How an agent works a ticket end-to-end |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Server runbook, outbound install, backup/restore |
| [docs/TERMINOLOGY.md](docs/TERMINOLOGY.md) | Ray-gated, seat, lane, cycle, digest, binding, Blocked by Ray |
| [DESIGN-v1.md](DESIGN-v1.md) | v1 definition, architecture, acceptance checklist |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branch/commit/review process, Ray-gated promotion |
| [SECURITY.md](SECURITY.md) | Security posture and report scope |

## Non-goals (v1)

Multi-user auth, multi-writer replication, attachments, search beyond
SQLite LIKE, real-time push (the UI polls), platforms other than
Matrix/Discord/Slack.
