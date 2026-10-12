# ticket-hub HTTP API

Base URL: `http://127.0.0.1:8777` (loopback; port from `$TICKET_HUB_PORT`).
All request/response bodies are JSON unless noted. Interactive OpenAPI docs
at `/docs` (FastAPI default).

## Conventions

### Identifiers
Prefixed ULIDs, lexicographically sortable: `TK-…` ticket, `EV-…` event,
`ED-…` edge, `TB-…` thread binding.

### Actors
Any `owner`/`actor` field accepts a string or an object:

```json
{"name": "hermes", "id": "hermes", "family": "hermes", "kind": "agent"}
```

Strings are coerced (`"ray"` → `{"id": "ray", "name": "ray", "family":
"unknown", "kind": "agent"}`); `kind` ∈ `agent | human | system`.

### Error envelope
Every error (HTTP 4xx/5xx) is:

```json
{"error": {"code": "not_found", "message": "Ticket TK-9 nope not found"}}
```

Validation failures add a `details` array (FastAPI error list). Known
codes: `not_found` (404), `bad_request` (400 — unknown kind/type/platform,
illegal transition, empty patch), `cycle_rejected` (400 — parent_of/blocks
edge would cycle), `validation` (422), `unsupported` (501 — store does not
expose bindings), `store_unavailable` (503), `error` (generic).

### Enumerations
- Statuses (canonical lowercase, case-insensitive on input):
  `draft, open, running, blocked, review, done, cancelled`
- Edge types: `parent_of, blocks, relates_to, duplicate_of`
  (`parent_of` and `blocks` are cycle-checked)
- Event kinds: `created, updated, status_changed, comment, checkpoint,
  decision, approval_receipt, artifact, handoff, thread_message, link`
- Platforms: `matrix, discord, slack, fake` (`fake` is test-only)

## Ticket CRUD

### `POST /tickets` → 201
```json
{"title": "Fix flaky replay", "body": "see logs", "parent": "TK-…",
 "owner": "hermes", "tags": ["qa"], "families": ["hermes"],
 "priority": "high", "external_refs": {}}
```
`families` defaults to the owner's family when omitted (if known). New
tickets start at `draft`. Returns the Ticket.

Ticket shape:
```json
{"id": "TK-01ABC…", "title": "Fix flaky replay", "body": "see logs",
 "status": "draft", "priority": "medium",
 "owner": {"id": "hermes", "name": "hermes", "family": "hermes",
           "kind": "agent"},
 "tags": ["qa"], "families": ["hermes"], "external_refs": {},
 "artifacts": [], "created_at": "2026-10-11T02:00:00+00:00",
 "updated_at": "2026-10-11T02:00:00+00:00"}
```

### `GET /tickets`
Query params (all optional, AND-combined): `status` (case-insensitive;
unknown status → 400), `family`, `tag`, `q` (SQLite LIKE substring).
Returns a JSON array of Tickets.

### `GET /tickets/{id}`
Returns the Ticket, or 404 `not_found`.

### `PATCH /tickets/{id}`
```json
{"status": "running", "priority": "high", "title": null, "body": null,
 "actor": {"name": "glm", "family": "glm"}}
```
At least one of `status/priority/title/body` required (else 400). Status
changes go through the store's transition table — an illegal move (e.g.
`draft → done`) is 400 `bad_request: illegal status transition`.

## Events (append-only)

### `POST /tickets/{id}/events` → 201
```json
{"kind": "checkpoint", "actor": "hermes",
 "payload": {"remaining": ["wire the exporter", "backfill dry-run"]}}
```
Unknown `kind` → 400. Events are **immutable**: there is no update or
delete endpoint, and DB triggers enforce append-only at the store. Returns:
```json
{"id": "EV-01ABC…", "ticket_id": "TK-01ABC…",
 "ts": "2026-10-11T02:05:00+00:00", "seq": 7,
 "actor": {"id": "hermes", "name": "hermes", "family": "hermes",
           "kind": "agent"},
 "kind": "checkpoint", "payload": {"remaining": ["wire the exporter"]}}
```

Conventions inside payloads:
- `comment` → `{"text": "…"}`
- `checkpoint` → optional `remaining: [str]` list, surfaced by `/plan`
  under `remaining_notes`
- `RAY-GATED` → `{"marker": "RAY-GATED", "note": "…"}` flags an item as
  **Blocked by Ray** in `/plan` (or set status `blocked`)

## Edges

### `POST /tickets/{id}/edges` → 201
```json
{"type": "parent_of", "dst": "TK-01XYZ…", "meta": {}}
```
Both endpoints must exist (404 otherwise); unknown type → 400; a
`parent_of`/`blocks` edge that would create a cycle → 400
`cycle_rejected`. Returns `{"id": "ED-…", "src": "TK-…", "dst": "TK-…",
"type": "parent_of", "meta": {}}`.

## Projections

### `GET /tickets/{id}/lineage`
Everything about this ticket's neighborhood:
```json
{"ticket": { … },
 "ancestry": [ …root→parent Tickets… ],
 "descendants": [ …all child-subtree Tickets… ],
 "related": [ …edges touching this ticket… ],
 "tree": {"ticket": { … }, "children": [ {"ticket": …, "children": []} ]},
 "events": [ …all events in the subtree, sorted by (ts, id)… ]}
```

### `GET /tickets/{id}/plan`
The three-questions view for a project ticket:
```json
{"ticket": { … },
 "summary": "body of the ticket",
 "progress": {"done": 3, "open": 2, "total": 5, "percent": 60},
 "remaining_items": [{"id": "TK-…", "title": "…", "status": "open",
                      "owner": "hermes"}],
 "remaining_notes": ["wire the exporter"],
 "blocked_by_ray": [{"id": "TK-…", "title": "…", "status": "blocked"}],
 "recent_activity": [ …last 8 events on this ticket… ]}
```
- `progress.percent` = done descendants / (descendants − cancelled); a leaf
  with no work items reports 100 if done else 0.
- `remaining_notes` come from the newest `checkpoint` event carrying
  `payload.remaining` anywhere in the subtree.
- `blocked_by_ray` = descendants with status `blocked` **or** a
  `{"marker": "RAY-GATED"}` event.

### `GET /tickets/{id}/digest?format=json|md&full=false`
Reader-facing translation of the subtree's events. Every bullet cites the
event ids it paraphrases (no-fabrication property, test-enforced).
`full=true` uses the canonical translation layer (all event kinds); the
default is the compact status/artifact/decision/approval pass.
```json
{"project": "TK-01ROOT…", "project_title": "Ship the exporter",
 "ticket": "TK-01ABC…", "updated_at": "…",
 "bullets": [{"text": "advanced to Running", "event_ids": ["EV-01…"]}]}
```
`format=md` returns `text/markdown`:
```markdown
# Updates — Ship the exporter
- advanced to Running (EV-01ABC…)
```

### `GET /board`
```json
{"columns": {"draft": […], "open": […], "running": […], "blocked": […],
             "review": […], "done": […], "cancelled": […]},
 "child_counts": {"TK-01ABC…": 3}}
```
All seven status keys are always present (possibly empty arrays).

## Threads / chat bridges

### `POST /tickets/{id}/bind` → 201
```json
{"platform": "matrix", "room_id": "!room:example.invalid",
 "thread_id": "$threadroot", "root_msg_id": "$rootmsg"}
```
Unknown platform → 400. Returns the ThreadBinding (`id: TB-…`).

### `GET /tickets/{id}/bindings`
Array of ThreadBindings for the ticket. 501 `unsupported` if the store
does not expose bindings.

### `POST /adapters/{platform}/inbound`
Called by chat bridges when a reply lands in a bound thread:
```json
{"room_id": "!room:example.invalid", "thread_id": "$threadroot",
 "root_msg_id": "$reply", "actor": {"name": "ray", "kind": "human"},
 "text": "ship it"}
```
Resolves the binding (404 `not_found` if none) and appends a
`thread_message` event to the bound ticket. Returns
`{"event": { … }, "posted": false}` (`posted` is the reply-post-back flag;
inert in v1).

## UI

- `GET /` serves the SPA (Board / Graph / Ticket drawer / Updates).
- Static assets under `/ui`.

## Notes

- One writer: SQLite WAL, single process. Do not run two servers against
  the same `hub.db`.
- The store path is `$TICKET_HUB_DB` (default
  `~/tp-data/ticket-hub/hub.db`).
