#!/usr/bin/env bash
# Hermes VPS deploy. Runs on the host as user `hermes`:
#   deploy.sh <commit-sha> [deploy-id]
# The workflow runs `docker login ghcr.io` first (short-lived token) and logs out after.
# Flow: guard -> pull -> tag current as previous -> restart -> health gate
#       -> on failure restore previous (or stop if none) -> prune old images.
# Exit codes: 0 deployed and healthy; 1 deploy failed (rolled back / stopped); 2 refused.
# Env overrides (tests): HERMES_ROOT, IMAGE_REPO, PORT, HEALTH_TIMEOUT, HEALTH_INTERVAL.
# Prints no secret values; deploy.env holds none.
set -uo pipefail

ROOT="${HERMES_ROOT:-/srv/hermes}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/loomalpha-labs/hermes-agent}"
PORT="${PORT:-8080}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
ENV_FILE="$ROOT/deploy.env"
CUR_REF_FILE="$ROOT/current_ref"
LOCAL_CURRENT="hermes-agent:current"
LOCAL_PREVIOUS="hermes-agent:previous"

log() { echo "==> $*"; }
sc() { sudo -n systemctl "$@"; }

# 0. Two-poller guard. Must run before anything is pulled or restarted.
if [ ! -f "$ROOT/RAILWAY_STOPPED" ]; then
  echo "refusing: Railway poller may be running (create $ROOT/RAILWAY_STOPPED only after Railway is stopped)" >&2
  exit 2
fi

SHA="${1:-}"
DEPLOY_ID="${2:-$SHA}"
[[ "$SHA" =~ ^[0-9a-f]{7,40}$ ]] || { echo "usage: deploy.sh <commit-sha> [deploy-id]" >&2; exit 2; }
[[ "$DEPLOY_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "invalid deploy id" >&2; exit 2; }
NEW_REF="$IMAGE_REPO:$SHA"

write_env() {  # write_env <sha> <id>; atomic, no secrets
  local tmp="$ENV_FILE.tmp.$$"
  {
    echo "IMAGE=$LOCAL_CURRENT"
    echo "IMAGE_REF=$NEW_REF"
    echo "PORT=$PORT"
    echo "DEPLOY_PLATFORM=vps"
    echo "DEPLOY_COMMIT_SHA=$1"
    echo "DEPLOY_ID=$2"
    echo "DEPLOY_REPLICA_ID=$(hostname)"
  } >"$tmp" && chmod 644 "$tmp" && mv "$tmp" "$ENV_FILE"
}

# 1. Pull first: a failed pull changes nothing.
log "pull $NEW_REF"
docker pull "$NEW_REF" || { echo "pull failed; nothing changed" >&2; exit 1; }

# 2. Remember what is running so we can roll back.
had_current=0
if docker image inspect "$LOCAL_CURRENT" >/dev/null 2>&1; then
  had_current=1
  docker tag "$LOCAL_CURRENT" "$LOCAL_PREVIOUS"
fi
prev_ref=""
[ -f "$CUR_REF_FILE" ] && prev_ref="$(cat "$CUR_REF_FILE")"
[ -f "$ENV_FILE" ] && cp -p "$ENV_FILE" "$ENV_FILE.prev"
docker tag "$NEW_REF" "$LOCAL_CURRENT"
write_env "$SHA" "$DEPLOY_ID"

# 3. Restart and poll health.
log "restart hermes ($SHA)"
healthy=0
if sc restart hermes; then
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while :; do
    if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:${PORT}/health/ready"; then
      healthy=1; break
    fi
    [ "$(date +%s)" -ge "$deadline" ] && break
    sleep "$HEALTH_INTERVAL"
  done
fi

# 4. Failure: restore previous, or stop if there was none.
if [ "$healthy" -ne 1 ]; then
  echo "health check failed for $SHA" >&2
  if [ "$had_current" -eq 1 ]; then
    log "rollback to $LOCAL_PREVIOUS"
    docker tag "$LOCAL_PREVIOUS" "$LOCAL_CURRENT"
    if [ -f "$ENV_FILE.prev" ]; then mv "$ENV_FILE.prev" "$ENV_FILE"; else write_env "unknown" "rollback"; fi
    sc restart hermes || echo "WARNING: restart of previous image failed" >&2
    docker rmi "$NEW_REF" >/dev/null 2>&1 || true
  else
    log "no previous image; stopping hermes"
    sc stop hermes || true
  fi
  exit 1
fi

# 5. Success: record the running ref and prune all but 2 tags (new + previous).
echo "$NEW_REF" >"$CUR_REF_FILE"
rm -f "$ENV_FILE.prev"
log "prune old images"
docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE_REPO" 2>/dev/null |
  while IFS= read -r ref; do
    [ -z "$ref" ] && continue
    [ "$ref" = "$NEW_REF" ] && continue
    [ -n "$prev_ref" ] && [ "$ref" = "$prev_ref" ] && continue
    docker rmi "$ref" >/dev/null 2>&1 || true
  done
log "deployed $SHA"
exit 0
