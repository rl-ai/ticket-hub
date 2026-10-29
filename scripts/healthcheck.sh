#!/usr/bin/env bash
# healthcheck.sh — one-line ok/FAIL for launchd/cron monitors.
# Checks: GET /board responds within timeout and the board has >0 tickets.
#   URL: ${TICKET_HUB_URL:-http://127.0.0.1:8777}
# Side effect: writes last-known health to
#   ${TICKET_HUB_HEALTH_STATUS:-$TICKET_HUB_HOME/health-status.json}
#   so the hub's /health endpoint and any monitor can surface failures
#   between checks (GLM review requirement: a FAIL must be visible).
# Exit: 0 ok, 1 FAIL.
set -uo pipefail

URL="${TICKET_HUB_URL:-http://127.0.0.1:8777}"
TIMEOUT="${TICKET_HUB_HEALTHCHECK_TIMEOUT:-5}"
STATUS_FILE="${TICKET_HUB_HEALTH_STATUS:-${TICKET_HUB_HOME:-$HOME/.ticket-hub}/health-status.json}"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

write_status() {  # $1 ok|fail  $2 tickets  $3 error(json or null)
    mkdir -p "$(dirname "${STATUS_FILE}")" 2>/dev/null || true
    printf '%s' "{\"ok\": $1, \"ts\": \"${TS}\", \"tickets\": $2, \"url\": \"${URL}\", \"last_error\": $3}" \
        > "${STATUS_FILE}.tmp" 2>/dev/null && mv "${STATUS_FILE}.tmp" "${STATUS_FILE}" 2>/dev/null || true
}

BODY="$(curl -fsS --max-time "${TIMEOUT}" "${URL}/board" 2>/dev/null)" || {
    echo "FAIL  hub healthcheck: no response from ${URL}/board within ${TIMEOUT}s"
    write_status false -1 '"no response"'
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
    write_status true "${COUNT}" null
    exit 0
else
    echo "FAIL  hub healthcheck: ${URL}/board unhealthy (tickets=${COUNT:-unparseable})"
    write_status false "${COUNT:--1}" '"unhealthy board"'
    exit 1
fi
