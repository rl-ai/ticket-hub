# Agent guide: working a ticket end-to-end

Every agent (hermes, forge, glm, eva, …) works the same hub through
`hub --json` (or `hub_client.HubClient` directly — stdlib only). This is
the full loop: find work, claim it, record milestones, escalate when
stuck, report for standup, finish.

## Rules first

1. **Events are immutable.** Never edit or delete history — there is no
   edit/delete API and the store enforces append-only. Correct the record
   by appending a new event.
2. **One ticket per task.** Don't pile unrelated work onto a ticket, and
   don't reopen finished (terminal) tickets — `done`/`cancelled` are
   terminal; create a follow-up and `hub link` it.
3. **Blocked = needs someone.** Set `blocked` only when you are genuinely
   waiting on a person or another ticket, and say why in a comment.
   Waiting on Ray's approval specifically = **Blocked by Ray**: set status
   `blocked` and/or append
   `hub event ID decision --json '{"marker": "RAY-GATED", "note": "why"}'`
   — either makes it show up in the parent's `plan.blocked_by_ray`.
4. **Attribute yourself.** Pass `--actor <your-name>` on writes so the
   timeline shows who did what.
5. **Report facts, not vibes.** Progress comes from child statuses and
   events; a comment claiming "90% done" changes nothing. Use `hub plan`.

## The loop

### 1. Find work
```bash
hub ls --status open --family hermes --json      # assigned, unclaimed
hub board --json                                 # everything, by column
```
Pick the highest-priority `open` ticket that is yours (or an unowned child
under your project). Don't grab a ticket another agent has already moved
to `running`.

### 2. Claim it
```bash
hub start TK-01ABC… --actor hermes   # draft→open→running (legal hops)
```

### 3. Work — record milestones as events
```bash
# milestones (comments are fine; use typed kinds when they fit)
hub comment TK-01ABC… "parser landed, 40 tests green" --actor hermes
hub event TK-01ABC… artifact --json '{"name": "exporter-v1.patch"}' --actor hermes
hub event TK-01ABC… decision --json '{"decision": "CSV over Parquet, see rationale comment"}' --actor hermes
```
If you learn what's still left, record it so `plan` surfaces it:
```bash
hub event TK-01ABC… checkpoint --json '{"remaining": ["backfill dry-run"]}' --actor hermes
```

### 4. Stuck? Block with a reason
```bash
hub comment TK-01ABC… "need prod creds before backfill" --actor hermes
hub set TK-01ABC… --status blocked --actor hermes
# if it is Ray's approval you need, flag it explicitly:
hub event TK-01ABC… decision --json '{"marker": "RAY-GATED", "note": "approve prod creds"}' --actor hermes
```
Blocked tickets surface in `hub plan` on the parent under BLOCKED BY RAY
(and in the board's `blocked` column). Unblock with
`hub set … --status running`.

### 5. Standup / status questions
```bash
hub plan TK-01ROOT… --json | jq '{progress, blocked_by_ray, remaining_items}'
hub digest TK-01ROOT… --md          # human-readable, event-cited
```
Three answers: what it is (`summary`), how far along (`progress.percent`,
computed from child statuses), what's left (`remaining_items` +
`remaining_notes` + `blocked_by_ray`).

### 6. Finish
```bash
hub comment TK-01ABC… "shipped; dry-run clean on 2026-10-11" --actor hermes
hub done TK-01ABC… --actor hermes   # running→review→done
```
Review/approval is a human decision — blocked by Ray — an agent moving a
ticket through `review → done` does not create acceptance authority;
acceptance belongs to Ray.

## Subagent / fan-out pattern

Big task? Make it a project:
```bash
root=$(hub new "Migrate ledger" --body "Migrate X; plan in children" \
        --owner ray --json | jq -r .id)
for t in "Extract" "Transform" "Load" "Verify"; do
  hub new "$t" --parent "$root" --owner hermes
done
```
The parent's body is the plan summary; the children are the plan. Work
each child with the loop above; `hub plan $root` answers status questions
for all of them at once.

## Error handling

- Any failure: exit 1, stderr `error: <code>: <message>`.
- `connection_error` → server down; retry later, don't spin.
- `bad_request: illegal status transition` → you skipped a state; use
  `hub start`/`hub done` which hop legally.
- `cycle_rejected` → your `parent_of`/`blocks` edge would loop; re-parent.
- `not_found` → wrong id or deleted-elsewhere; re-`ls`.
