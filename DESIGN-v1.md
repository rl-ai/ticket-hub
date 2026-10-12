# ticket-hub — v1 Definition & Design

Status: v0 built and running; v1 = CLI + outbound sync + this document.
Owner: Ray. Build authority: hermes orchestrator + lanes; acceptance: Ray.

## End state (deliverable definition)

ticket-hub is THE collaboration hub for Ray's multi-agent operation: one
central, durable place where every request/project lives as a typed object
graph, worked by agents across families (hermes, forge, glm, eva, ray),
mirrored into the chat threads people actually use, and readable by humans
as marketing-grade updates that are provably derived from real events.

A working v1 means ALL of:

1. **Object graph store** — Ticket/Actor/Edge/Event/ThreadBinding;
   parent-child hierarchy (cycle-free, single parent), blocks / relates_to /
   duplicate_of edges; append-only immutable history (DB-trigger enforced);
   SQLite WAL, one writer.  [DONE v0]
2. **HTTP API** — full CRUD + projections (lineage, board, digest) with a
   stable JSON contract and JSON error envelope.  [DONE v0]
3. **Human UI** — build-step-free SPA: Board (status columns), Graph
   (collapsible hierarchy), Ticket drawer (timeline + lineage breadcrumb +
   bound threads), Updates (digest view with event-id citations).  [DONE v0]
4. **Terminal/agent surface** — `hub` CLI + stdlib `HubClient`: every verb
   (new/get/ls/set/comment/event/link/lineage/digest/board/bind/done/start),
   `--json` for machines, exit codes for scripts, so ANY agent family drives
   the same hub with zero deps.  [v1 lane CLI]
5. **Chat compatibility** — one ticket = thread root, child ticket =
   subthread, on Element/Matrix, Discord, Slack:
   - inbound: chat reply → `thread_message` event on the bound ticket, merged
     into its lineage/timeline.  [DONE v0, live-verified]
   - outbound: ticket updates → digest bullets posted into the bound thread
     (child under parent's root), with watermark dedupe so nothing reposts.
     [v1 lane OUTBOUND + launchd installer]
   - secrets live only in runtime config (`~/tp-data/ticket-hub/adapters.json`,
     chmod 600); adapters construct inert with zero credentials.  [DONE v0]
6. **Readable translation** — deterministic template pass maps raw events to
   reader sentences; every bullet cites the event ids it paraphrases
   (no-fabrication property, test-enforced); optional GLM polish pass
   injected as a callable, falls back to template on any failure or invented
   citation.  [DONE v0; polish wiring in OUTBOUND lane]
7. **Multi-family access control** — hub is loopback-only in v1
   (127.0.0.1): agents reach it via their own host tools; actors carry
   `family` chips; every mutation is attributed. Network auth is deferred
   until a non-loopback consumer exists (then bearer-per-actor, not shared).
8. **Durability & operations** — persisted launchd-style lifecycle for API +
   outbound daemon; SQLite file backed up in normal tp-data rotation;
   migration from the legacy flat ledger done idempotently with full history.
   [migration DONE; launchd templates in v1 lanes]
9. **Adopted as the work OS** — forge cycles and orchestrator file tickets
   against the hub API (not the flat ledger); hub.db is the go-forward
   ledger; `~/tp-data/tickets/tickets.db` frozen as source-of-history.

Explicit non-goals for v1: multi-user auth, multi-writer replication,
attachments/blob storage, search index beyond SQLite LIKE, real-time push
(UI polls), non-Matrix/Discord/Slack platforms.

## Architecture (as built)

```
agents ── hub CLI / HubClient (stdlib) ─┐
browsers ── SPA (board/graph/drawer/updates) ── FastAPI :8777 (loopback)
forge/orchestrator ── HTTP ──────────────────┘        │
                                    StoreGateway → Store (SQLite WAL, ~/tp-data/ticket-hub/hub.db)
                                                     │ append-only events (trigger-enforced)
                     inbound webhook POST /adapters/{platform}/inbound
                     outbound OutboundPoller ── adapters (matrix|discord|slack|fake)
                                                 config: ~/tp-data/ticket-hub/adapters.json (600)
                                                 state:  outbound_state.json (atomic write)
```

- **Contract-first lanes**: lanes build against DESIGN.md signatures in
  isolated sandbox worktrees; integration reconciles via a gateway facade;
  the whole board (core+api+adapters+cli+outbound) must be green before the
  candidate ships.
- **Status lifecycle** (canonical lowercase): draft → open → running →
  {blocked|review} → done|cancelled; transitions enforced at the store.
- **Thread semantics**: Matrix = `m.relates_to` thread root; Discord =
  thread per ticket under parent's root message; Slack = `thread_ts`
  anchoring. Child tickets reply under the parent binding's root.
- **Digest honesty**: template pass only cites ids present in input; polish
  can rewrite prose but never add citations; violations fall back whole.

## Acceptance checklist (v1 → "working version")

- [x] Server live on :8777; UI serves; demo + migrated real history visible
- [x] 90 tests green (core 23, api 19, translate 24, adapters 24)
- [x] Real-store e2e: hierarchy, cycle rejection (both paths), lifecycle
      enforcement, thread inbound → lineage merge, digest both modes
- [x] Legacy ledger migration applied (37 tickets / 33 edges / 236 events,
      ts+actor+ray-gated preserved, idempotent, verified through live API)
- [ ] CLI lane: verbs + exit codes + `--json`; full repo board green
- [ ] OUTBOUND lane: watermark posting, inert-safe, launchd template; full
      repo board green
- [ ] Integration merge + final e2e (CLI drives live server; outbound tick
      posts to FakeAdapter end-to-end)
- [ ] Ray acceptance

## Operational runbook

- Server (persisted): `cd ~/tp-data/forge/sandbox/hub-integ && python api.py`
  → `:8777`, DB `~/tp-data/ticket-hub/hub.db`.
- CLI: `hub ls --status running`, `hub new "Title" --owner ray --tag forge`,
  `hub digest <ID> --md`, `hub link A B parent_of`.
- Outbound: `python outbound.py --once` (dry loop), installer prints the
  launchctl command — service control stays Ray's.
- Adapters config: `~/tp-data/ticket-hub/adapters.json`, per-platform
  sections; absent section ⇒ inert. Never commit tokens.
- Promotion path: hub work lives on branch `hub` (pushed to Morpheus
  origin); merges into `main` are Ray-gated like forge cycles.
