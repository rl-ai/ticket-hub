# hub CLI

`hub` is the terminal client for the ticket-hub API: one compact line per
ticket for humans, `--json` for machines, exit codes for scripts. The
`bin/hub` shim is PATH-installable (symlink it into `~/.local/bin`); it runs
`hub_cli.py` with the forge venv python when available, else `python3`.
The underlying client (`hub_client.py`) is stdlib-only — any python3 can
import it with no venv or pip.

## Global flags

| Flag | Meaning |
|---|---|
| `--url URL` | Hub base URL (default: `$TICKET_HUB_URL` or `http://127.0.0.1:8777`) |
| `--timeout N` | HTTP timeout in seconds (default 30) |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Any hub error — `error: <code>: <message>` on stderr (API error or `connection_error`) |

## Verbs

### `hub new TITLE [--body B] [--parent ID] [--owner NAME] [--tag TAG]… [--priority P] [--json]`
Create a ticket (starts at `draft`). `--tag` repeats. `--json` prints the
full created ticket as raw JSON; otherwise one line:
```
$ hub new "Fix flaky replay" --body "see logs" --tag qa --owner hermes
TK-01ABC…  draft  Fix flaky replay  [hermes]
```

### `hub get ID [--json]`
Show one ticket (line or full JSON).

### `hub ls [--status S] [--family F] [--tag T] [--q SUB] [--json]`
List tickets; filters AND-combine; `--q` is a substring match.
```
$ hub ls --status running --json
[ { "id": "TK-…", … } ]
```

### `hub set ID [--status S] [--priority P] [--title T] [--body B] [--actor A]`
Patch fields. At least one field flag required. Status changes must be
legal transitions (illegal → exit 1):
```
$ hub set TK-01ABC… --status blocked --actor hermes
TK-01ABC…  blocked  Fix flaky replay  [hermes]
```

### `hub comment ID TEXT [--actor A]`
Append a `comment` event. Prints `EV-…  comment  on TK-…`.

### `hub event ID KIND [--json JSON_OBJECT] [--actor A]`
Append a typed event (`comment`, `checkpoint`, `decision`, `artifact`,
`handoff`, …). Note: on this verb `--json` carries the **payload**, not the
output format:
```
$ hub event TK-01ROOT… checkpoint --json '{"remaining": ["backfill dry-run"]}'
EV-01ABC…  checkpoint  on TK-01ROOT…
```
To flag something for Ray: `hub event ID decision --json
'{"marker": "RAY-GATED", "note": "awaiting approval"}'`.

### `hub link SRC DST TYPE`
Add an edge: `parent_of | blocks | relates_to | duplicate_of`. Cycle-guarded:
```
$ hub link TK-01ROOT… TK-01CHILD… parent_of
ED-01ABC…  TK-01ROOT…  -parent_of->  TK-01CHILD…
```

### `hub lineage ID [--json]`
Ancestry chain, child/related/event counts (or the full subtree JSON).

### `hub plan ID [--json]`
What this ticket is about, progress, what remains (see docs/API.md for the
JSON shape):
```
$ hub plan TK-01ROOT…
TK-01ROOT…  [running]  Ship the exporter

  Export widget data nightly

  progress  [###############.......] 60%  (3/5 done)

  BLOCKED BY RAY (1):
    ! TK-01XYZ…  [blocked]  Approve production credentials

  REMAINING (2):
    - TK-01ABC…  [open]  Wire the exporter  [hermes]
    … 1 more

  RECENT:
    2026-10-11T02:05:00  hermes: checkpoint
```

### `hub digest ID [--md] [--json] [--full]`
Translated project digest; every bullet cites event ids. `--md` → markdown,
`--json` → raw JSON, `--full` → canonical translation (all event kinds).
Default: plain-text bullets on stdout.

### `hub board [--json]`
Tickets grouped by status column, with child counts:
```
== running (2) ==
TK-01ABC…  running  Ship the exporter  [ray]  (5 children)
```

### `hub bind ID PLATFORM ROOM [--THREAD T] [--ROOT MSG]`
Bind a chat thread to a ticket (`matrix | discord | slack`):
```
$ hub bind TK-01ABC… matrix '!room:example.invalid' --root '$rootmsg'
TB-01ABC…  bound  matrix:!room:example.invalid
```

### `hub start ID [--actor A]`
Move to `running`, hopping legally through `open` if the ticket is `draft`.

### `hub done ID [--actor A]`
Move to `done`, hopping legally through `open`/`review` as needed
(e.g. `running → review → done`).

## Scripting pattern

```bash
set -e
id=$(hub new "Nightly export" --owner hermes --json | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["id"])')
hub start "$id"
hub comment "$id" "schema locked" --actor hermes
hub done "$id"
```

Anything that fails prints `error: <code>: <message>` and exits 1 — branch
on the exit code, parse `--json` on stdout, never on stderr.
