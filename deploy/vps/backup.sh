#!/usr/bin/env bash
# Nightly off-site backup of /srv/hermes/data to the restic repo in /etc/hermes/backup.env
# (Cloudflare R2 — a different company from the VPS host, so one provider failure cannot take both).
#
# SQLite databases are never read live: each *.db is copied first with `sqlite3 .backup`, which takes a
# consistent snapshot while Jesse keeps writing. Live *.db / *-wal / *-shm files are excluded and the
# consistent copies are backed up from the staging dir instead.
#
# On success touches /var/lib/hermes-backup/last-success (check.sh alerts if it is older than 36 h).
# Prints no secret values. Run as root.
set -euo pipefail

DATA=/srv/hermes/data
STAGE=/srv/hermes/backup-staging
STAMP_DIR=/var/lib/hermes-backup
ENV_FILE=/etc/hermes/backup.env

[ -r "$ENV_FILE" ] || { echo "backup: $ENV_FILE missing" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

log() { echo "[backup $(date -u +%H:%M:%SZ)] $*"; }

# Paths skipped everywhere: rebuildable caches and bundled package trees.
EXCLUDES=(
  --exclude "$DATA/.cache"
  --exclude "$DATA/.npm"
  --exclude "$DATA/.hermes/home/.cache"
  --exclude "$DATA/.hermes/state/dashboard-portable"
  --exclude "**/node_modules"
  --exclude "*.sock"
)

# 1. Consistent SQLite copies.
rm -rf "$STAGE"; install -d -m 700 "$STAGE"
n=0; fallback=0
while IFS= read -r -d '' db; do
  rel="${db#"$DATA"/}"
  dest="$STAGE/sqlite/$rel"
  mkdir -p "$(dirname "$dest")"
  if sqlite3 "$db" ".timeout 30000" ".backup '$dest'" 2>/dev/null; then
    n=$((n + 1))
  else
    cp -p "$db" "$dest"; fallback=$((fallback + 1))   # not SQLite, or locked: plain copy
  fi
done < <(find "$DATA" \( -path "$DATA/.cache" -o -path "$DATA/.npm" -o -path "$DATA/.hermes/home/.cache" \
          -o -path "$DATA/.hermes/state/dashboard-portable" -o -name node_modules \) -prune \
          -o -type f -name '*.db' -print0)
log "sqlite snapshots: $n consistent, $fallback plain copies"

# 2. Upload. Live database files are excluded; their consistent copies come from $STAGE.
restic backup --quiet --tag nightly --host hermes-vps \
  "${EXCLUDES[@]}" --exclude "$DATA/**/*.db" --exclude "*.db-wal" --exclude "*.db-shm" \
  "$DATA" "$STAGE"
log "snapshot uploaded"

# 3. Retention.
restic forget --quiet --host hermes-vps --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune
log "retention applied"

# 4. Weekly integrity check (Sunday): verify structure plus a 1% sample of the data.
if [ "$(date -u +%u)" = 7 ]; then
  restic check --read-data-subset=1% >/dev/null
  log "weekly check ok"
fi

rm -rf "$STAGE"
install -d -m 755 "$STAMP_DIR"
touch "$STAMP_DIR/last-success"
log "done"
