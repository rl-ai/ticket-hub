#!/bin/bash
# backup_to_unas.sh — nightly Eva -> Unas backup (ticket-hub DB + forge snapshots).
# Runs on Eva; target is the UniFi Drive share mounted at ~/nas/eva.
# Inert-safe: skips (exit 0) when the mount is absent, retries nothing.
set -u
DEST_BASE="$HOME/nas/eva/backups"
HUB_DB="$HOME/.local/share/ticket-hub/ticket-hub/hub.db"
SNAP_DIR="$HOME/.local/share/ticket-hub/forge/snapshots"
LOG="$HOME/.local/share/ticket-hub/ticket-hub/backup.log"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

if ! mount | grep -q "$HOME/nas/eva "; then
    log "SKIP: ~/nas/eva not mounted"
    exit 0
fi
mkdir -p "$DEST_BASE/ticket-hub" "$DEST_BASE/forge-snapshots"

# 1. hub DB: sqlite-consistent snapshot, keep 7 daily copies.
if [ -f "$HUB_DB" ]; then
    day=$(date +%Y%m%d)
    dest="$DEST_BASE/ticket-hub/hub-$day.db"
    if sqlite3 "$HUB_DB" ".backup '$dest'"; then
        log "OK hub db -> $dest ($(du -h "$dest" | cut -f1))"
        ls -1t "$DEST_BASE"/ticket-hub/hub-*.db 2>/dev/null | tail -n +8 \
            | xargs rm -f 2>/dev/null
    else
        log "FAIL hub db backup"
    fi
fi

# 2. forge snapshot bundles: rsync, --ignore-existing (bundles are immutable).
if [ -d "$SNAP_DIR" ]; then
    if rsync -a --ignore-existing "$SNAP_DIR/" "$DEST_BASE/forge-snapshots/" \
            --exclude '._*' 2>>"$LOG"; then
        find "$DEST_BASE/forge-snapshots" -name '._*' -delete 2>/dev/null
        log "OK snapshots -> $DEST_BASE/forge-snapshots"
    else
        log "FAIL snapshot rsync"
    fi
fi
exit 0
