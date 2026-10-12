#!/usr/bin/env bash
# healthcheck.sh — one-line ok/FAIL for launchd/cron monitors.
# Checks: GET /board responds within timeout and the board has >0 tickets.
#   URL: ${TICKET_HUB_URL:-http://127.0.0.1:8777}
# Exit: 0 ok, 1 FAIL.
set -uo pipefail

URL="${TICKET_HUB_URL:-http://127.0.0.1:8777}"
TIMEOUT="${TICKET_HUB_HEALTHCHECK_TIMEOUT:-5}"

BODY="$(curl -fsS --max-time "${TIMEOUT}" "${URL}/board" 2>/dev/null)" || {
    echo "FAIL  hub healthcheck: no response from ${URL}/board within ${TIMEOUT}s"
    exit 1
}

COUNT="$(python3 - "${BODY}" <<'EOF' 2>/dev/null
import json, sys
try:
    board = json.loads(sys.argv[1])
    cols = board.get("columns", {}) if isinstance(board, dict) else {}
    print(sum(len(v) for v in cols.values() if isinstance(v, list)))
except Exception:
    print(-1)
EOF
)"

if [ "${COUNT:-x}" != "x" ] && [ "${COUNT}" -gt 0 ] 2>/dev/null; then
    echo "ok  hub ${URL} — ${COUNT} tickets on board"
    exit 0
else
    echo "FAIL  hub healthcheck: ${URL}/board unhealthy (tickets=${COUNT:-unparseable})"
    exit 1
fi
