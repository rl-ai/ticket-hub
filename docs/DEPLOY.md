# Deploy runbook: ticket-hub

Two processes: the **API server** (`api.py`, FastAPI + SQLite) and the
**outbound daemon** (`outbound.py`, polls the hub and posts digests to bound
chat threads). Both run on the hub host; the daemon is optional until chat
mirroring is wanted.

## Environment variables

| Variable | Used by | Default |
|---|---|---|
| `TICKET_HUB_HOME` | all path defaults below | `~/.ticket-hub` |
| `TICKET_HUB_DB` | api.py | `~/.ticket-hub/hub.db` |
| `TICKET_HUB_PORT` | api.py | `8777` |
| `TICKET_HUB_URL` | hub CLI, outbound | `http://127.0.0.1:8777` |
| `TICKET_HUB_ADAPTERS_CONFIG` | install script → daemon `--config` | `~/.ticket-hub/adapters.json` |
| `TICKET_HUB_OUTBOUND_STATE` | install script → daemon `--state` | `~/.ticket-hub/outbound_state.json` |
| `TICKET_HUB_OUTBOUND_INTERVAL` | install script (seconds) | `60` |
| `TICKET_HUB_OUTBOUND_LOG_DIR` | install script | `~/.ticket-hub/logs` |
| `TICKET_HUB_PYTHON` | install script, hygiene check | `python3` on PATH |
| `TICKET_HUB_POLISH_URL` / `_MODEL` / `_KEY_FILE` | outbound (optional GLM polish) | unset = template-only digests |
| `TICKET_HUB_EXTRA_GATE_MARKERS` | plan projection / gate checks | unset; comma-list of additional legacy gate markers accepted alongside `OWNER-GATED` |
| `HUB_ACTOR` | hub CLI | unset; default actor name for `--actor`/`--actor-name` (else the OS user) |
| `HUB_BACKUP_DEST` | `scripts/backup_offsite.sh` | unset = skip; destination directory on an offsite/backup share |
| `HUB_BACKUP_EXTRA_DIR` | `scripts/backup_offsite.sh` | unset; optional directory of immutable files mirrored into `$HUB_BACKUP_DEST/extra` |

**Upgrading an existing install:** the default home is now `~/.ticket-hub`. If your data lives elsewhere, set
`TICKET_HUB_HOME` (or `TICKET_HUB_DB`) to it, or move the directory, before starting the server; otherwise the
hub starts against a fresh, empty database. Agents that still emit an older gate marker can keep it counted with
`TICKET_HUB_EXTRA_GATE_MARKERS`.

`TICKET_HUB_POLISH_KEY_FILE` points at a bearer-key file the daemon reads
**at call time only**; nothing secret is stored in the plist, the repo, or
the DB, and the key is never logged.

## API server

```bash
cd <repo>
python api.py                     # :8777 (port from $TICKET_HUB_PORT)
# or explicitly:
TICKET_HUB_DB=~/.ticket-hub/hub.db python api.py
```

- SQLite is WAL mode, **one writer** — never run two servers against the
  same `hub.db`.
- Clients (UI, CLI, outbound) use `http://127.0.0.1:8777`. The dev
  entrypoint binds `0.0.0.0` by default; if the host is on an untrusted
  network, front it with a loopback bind / firewall — see SECURITY.md.
- The parent ticket-hub directory is created automatically.

### Health check
```bash
curl -sf http://127.0.0.1:8777/board | head -c 200   # JSON with 7 status columns
curl -s  http://127.0.0.1:8777/tickets?status=running  # array
```
A healthy hub answers both; a dead one hangs / connection-refused.

### Restart
```bash
# find and stop
pkill -f "python api.py"          # or kill the PID from `lsof -i :8777`
# start again (from the repo dir)
cd <repo> && python api.py &
# verify
curl -sf http://127.0.0.1:8777/board >/dev/null && echo ok
```
The outbound daemon survives a hub restart: a tick whose hub call fails is
a silent no-op and retries next tick; watermarks only advance after
successful posts, so nothing is lost.

## Outbound daemon

One-shot loop (dry run — inert when no adapters are configured):
```bash
python outbound.py --once         # one tick and exit
python outbound.py --interval 60  # foreground loop
```

### Install as a user service (the owner runs the load command)
```bash
deploy/install_outbound.sh
```
The script renders `deploy/com.ticket-hub.outbound.plist.template` into
`~/Library/LaunchAgents/com.ticket-hub.outbound.plist`, lints it, and
**prints the exact command** — it never loads the service itself; service
control stays the owner's:
```bash
launchctl load   ~/Library/LaunchAgents/com.ticket-hub.outbound.plist
launchctl unload ~/Library/LaunchAgents/com.ticket-hub.outbound.plist
```

### Log paths (launchd)
```
~/.ticket-hub/logs/outbound.log      # stdout
~/.ticket-hub/logs/outbound.err.log  # stderr
```

### State file
`~/.ticket-hub/outbound_state.json` — per-ticket watermark + set of
already-posted event ids. Atomic writes (tmp + rename); deleting it only
loses the dedupe history (already-posted digests could repost once), it
never affects hub data.

## Adapters config

Location: `~/.ticket-hub/adapters.json` (the `--config` flag and
`TICKET_HUB_ADAPTERS_CONFIG` override it). `chmod 600`. **Never commit it.**
Shape — one section per platform; an absent section (or token) ⇒ that
adapter is inert (no network I/O, never crashes):

```json
{
  "matrix":  {"homeserver": "https://matrix.example.invalid",
              "token": "***", "user_map": {"@alice:example.invalid": "alice"}},
  "discord": {"token": "<discord-bot-token>"},
  "slack":   {"token": "<slack-bot-token>",
              "user_map": {"U0000000000": "alice"}}
}
```

Placeholders only — real tokens exist solely in this file. A malformed or
missing config makes the daemon start inert; a broken platform section is
skipped without taking the daemon down.

## Backup / restore of hub.db

The hub is one SQLite file (plus WAL sidecars) under
`~/.ticket-hub/` — it rides the normal backup rotation. To
take a consistent snapshot explicitly:

```bash
# backup (safe while the server runs — sqlite3 checkpoints the WAL into the copy)
sqlite3 ~/.ticket-hub/hub.db ".backup ~/.ticket-hub/backups/hub-$(date +%Y%m%d).db"

# restore: stop the server, swap the file, restart
pkill -f "python api.py"
cp ~/.ticket-hub/backups/hub-YYYYMMDD.db ~/.ticket-hub/hub.db
rm -f ~/.ticket-hub/hub.db-wal ~/.ticket-hub/hub.db-shm
cd <repo> && python api.py &
```

Events are append-only, so restoring an older DB rewinds history — restore
only when that is what you want. Verify after restore with
`hub ls --json | head` and `hub board`.

## Upgrades

Pull on the hub branch, restart the API server, reload the launchd service
if `outbound.py` changed. The schema migrates in place; the legacy flat-ledger
migration was a one-time import applied before release, and its script is no
longer shipped.
