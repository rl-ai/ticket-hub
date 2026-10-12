# Changelog

All notable changes to ticket-hub are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
milestones backfilled from the `hub` branch history (see git log for the
per-commit record).

## [Unreleased]

### Accepted (v2 Mission Control — accepted by Ray 2026-10-12)
- Wiki: append-only pages (revisions like events), `/wiki*` API, `hub wiki` CLI, `[[WikiLink]]` renderer, seed corpus (repo docs + ops knowledge; re-import freshness; machine actor `seed-import`).
- Mission Control UI: Board | Graph | Wiki | Monitoring | Admin | Roles.
- `GET /admin/status` read-only aggregate (health, services, backups, maintenance, hygiene, counts); hygiene/maintenance emit machine status files.
- Roles registry (7 charters) served at `/roles`; `hub packet` renders the ticket-as-shared-packet.
- `swarm.py`: deterministic harness — CAS claims (claim-then-confirm over append-only log), locked-DB bounded retry, watermark dedupe, INERT without config.
- GLM build review: PASS (no blockers; 4 advisories logged to root ticket, 2 deferred to v2.1).


### Accepted
- v1 accepted by Ray 2026-10-12 — hub becomes the go-forward work OS (GLM review PASS-CONDITIONAL, all blockers closed).

## [1.1] - 2026-10-11 — Plan view
### Added
- `GET /tickets/{id}/plan` endpoint and `hub plan` CLI verb.
- Drawer Plan section: progress %, remaining items, blocked-by-Ray.

## [1.0] - 2026-10-11 — CLI + outbound
### Added
- `hub` CLI (`hub_cli.py`) with stdlib `HubClient` (`hub_client.py`) and
  PATH-installable `bin/hub` shim: new/ls/start/done/plan verbs, `--json`
  machine output.
- Outbound poller daemon (`outbound.py`) with watermark dedupe, polish glue,
  and launchd installer (`deploy/install_outbound.sh`).
- Idempotent legacy-ledger → hub migration (`scripts/migrate_legacy_ledger.py`):
  37 tickets, 33 edges, 236 events with timestamps, actors, and ray-gating
  preserved.
- Integration complete: live end-to-end (CLI verbs + outbound dedupe), 133
  board.

## [0.1] - 2026-10-10 — UI + gateway migration
### Added
- FastAPI endpoints per DESIGN.md + single-page UI (board / graph / drawer /
  updates).
- Chat adapters: matrix / discord / slack / fake, with translate
  template/polish passes; adapters run inert when no config/token present.
- Gateway wired to the real `StoreGateway`; canonical lowercase statuses;
  real-store e2e verified; full digest via lane-C template_pass.

## [0.1] - 2026-10-09 — API + UI (first cut)
### Added
- FastAPI + UI branch landing (`hub-api`): endpoints per DESIGN.md and
  standalone FakeStore tests.

## [0.0] - 2026-10-09 — Core
### Added
- v0 design contract: object graph, lineage, translate, adapters, UI.
- Object model + SQLite WAL store: append-only events, cycle-safe hierarchy.
- Lineage projection with tests.
