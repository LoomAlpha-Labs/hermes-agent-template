#!/usr/bin/env bash
# Tests for deploy.sh with docker, sudo and curl stubbed on PATH (no root, no Docker).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0
SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

mkenv() {  # build a sandbox: stub bin dir, fake /srv/hermes root, call log
  local d; d="$(mktemp -d)"
  mkdir -p "$d/bin" "$d/root"
  : >"$d/log"
  cat >"$d/bin/docker" <<'S'
#!/usr/bin/env bash
echo "docker $*" >>"$STUB_LOG"
case "$1 $2" in
  "image inspect") [ "${STUB_HAS_CURRENT:-0}" = 1 ] && exit 0 || exit 1 ;;
esac
case "$1" in
  pull) [ "${STUB_PULL_FAIL:-0}" = 1 ] && exit 1 ;;
  tag) [ -n "${STUB_TAG_FAIL:-}" ] && [[ "$*" == *"$STUB_TAG_FAIL"* ]] && exit 1 ;;
  images) [ -f "$STUB_IMAGES" ] && cat "$STUB_IMAGES" ;;
esac
exit 0
S
  cat >"$d/bin/sudo" <<'S'
#!/usr/bin/env bash
echo "sudo $*" >>"$STUB_LOG"
[ "$1" = "-n" ] && shift
case "$*" in *is-active*) echo active ;; esac
exit 0
S
  cat >"$d/bin/curl" <<'S'
#!/usr/bin/env bash
echo "curl $*" >>"$STUB_LOG"
[ "${STUB_HEALTH:-200}" = 200 ] || exit 22
if [ -n "${STUB_BODY:-}" ]; then echo "$STUB_BODY"; else echo '{"status":"ready","commit_sha_prefix":"aaaaaaaaaaaa","fresh":true}'; fi
exit 0
S
  cat >"$d/bin/flock" <<'S'
#!/usr/bin/env bash
[ "${STUB_LOCK_FAIL:-0}" = 1 ] && exit 1
exit 0
S
  chmod +x "$d/bin/"*
  echo "$d"
}

runcase() {  # runcase <env dir> [extra env assignments...] -> sets rc, out
  local d=$1; shift
  out="$(env PATH="$d/bin:$PATH" STUB_LOG="$d/log" STUB_IMAGES="$d/images" \
    HERMES_ROOT="$d/root" HEALTH_TIMEOUT=1 HEALTH_INTERVAL=0.2 "$@" \
    "$HERE/deploy.sh" "$SHA" 12345 2>&1)"; rc=$?
}

check() {  # check <name> <condition-result 0/1>
  if [ "$2" -eq 0 ]; then pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi
}
has() { grep -qF -- "$2" "$1/log"; }

# 1. missing RAILWAY_STOPPED -> exit 2, exact message, nothing touched
d="$(mkenv)"
runcase "$d" STUB_HEALTH=200
[ "$rc" -eq 2 ]; check "guard exit code" $?
[[ "$out" == *"refusing: Railway poller may be running"* ]]; check "guard message" $?
[ ! -s "$d/log" ]; check "guard runs before any docker/systemctl call" $?

# 2. health 200 -> exit 0, deploy.env content, prune keeps 2 tags
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
echo ghcr.io/loomalpha-labs/hermes-agent:oldold >"$d/root/current_ref"
printf '%s\n' ghcr.io/loomalpha-labs/hermes-agent:$SHA ghcr.io/loomalpha-labs/hermes-agent:oldold \
  ghcr.io/loomalpha-labs/hermes-agent:older ghcr.io/loomalpha-labs/hermes-agent:oldest >"$d/images"
runcase "$d" STUB_HEALTH=200 STUB_HAS_CURRENT=1
[ "$rc" -eq 0 ]; check "success exit 0 ($out)" $?
grep -qx "DEPLOY_COMMIT_SHA=$SHA" "$d/root/deploy.env"; check "deploy.env has sha" $?
grep -qx "DEPLOY_PLATFORM=vps" "$d/root/deploy.env"; check "deploy.env platform" $?
grep -qx "DEPLOY_ID=12345" "$d/root/deploy.env"; check "deploy.env id" $?
grep -q "^DEPLOY_REPLICA_ID=." "$d/root/deploy.env"; check "deploy.env replica" $?
has "$d" "docker tag hermes-agent:current hermes-agent:previous"; check "current tagged previous" $?
has "$d" "sudo -n systemctl restart hermes" || has "$d" "sudo -n /usr/bin/systemctl restart hermes"; check "restart called" $?
has "$d" "docker rmi ghcr.io/loomalpha-labs/hermes-agent:older"; check "prune removes older" $?
has "$d" "docker rmi ghcr.io/loomalpha-labs/hermes-agent:oldest"; check "prune removes oldest" $?
! has "$d" "docker rmi ghcr.io/loomalpha-labs/hermes-agent:oldold"; check "prune keeps previous" $?
! has "$d" "docker rmi ghcr.io/loomalpha-labs/hermes-agent:$SHA"; check "prune keeps new" $?
[ "$(cat "$d/root/current_ref")" = "ghcr.io/loomalpha-labs/hermes-agent:$SHA" ]; check "current_ref updated" $?

# 3. health 503 -> exit 1, previous image restored and started
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
echo "PORT=8080
DEPLOY_COMMIT_SHA=oldsha" >"$d/root/deploy.env"
echo ghcr.io/loomalpha-labs/hermes-agent:oldold >"$d/root/current_ref"
runcase "$d" STUB_HEALTH=503 STUB_HAS_CURRENT=1
[ "$rc" -eq 1 ]; check "failed health exit 1" $?
has "$d" "docker tag hermes-agent:previous hermes-agent:current"; check "previous re-tagged" $?
[ "$(grep -c 'systemctl restart hermes\|systemctl start hermes' "$d/log")" -ge 2 ]; check "service started again on previous" $?
grep -qx "DEPLOY_COMMIT_SHA=oldsha" "$d/root/deploy.env"; check "deploy.env restored" $?
[ "$(cat "$d/root/current_ref")" = "ghcr.io/loomalpha-labs/hermes-agent:oldold" ]; check "current_ref unchanged" $?

# 4. health 503 on first ever deploy -> exit 1, service stopped (nothing to roll back to)
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
runcase "$d" STUB_HEALTH=503 STUB_HAS_CURRENT=0
[ "$rc" -eq 1 ]; check "first deploy failure exit 1" $?
has "$d" "systemctl stop hermes"; check "first deploy failure stops service" $?
has "$d" "docker rmi hermes-agent:current"; check "first deploy failure untags current" $?
[ ! -e "$d/root/deploy.env" ]; check "first deploy failure removes deploy.env" $?

# 5. pull failure -> exit 1, no restart
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
runcase "$d" STUB_PULL_FAIL=1
[ "$rc" -eq 1 ]; check "pull failure exit 1" $?
! has "$d" "systemctl"; check "pull failure leaves service alone" $?

# 5b. tag failures fail the deploy without touching the running service or deploy.env
for pat in "hermes-agent:current hermes-agent:previous" "ghcr.io/loomalpha-labs/hermes-agent:$SHA hermes-agent:current"; do
  d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
  echo "DEPLOY_COMMIT_SHA=oldsha" >"$d/root/deploy.env"
  runcase "$d" STUB_HAS_CURRENT=1 STUB_TAG_FAIL="$pat"
  [ "$rc" -eq 1 ]; check "tag failure exit 1 ($pat)" $?
  ! has "$d" "systemctl restart"; check "tag failure no restart ($pat)" $?
  grep -qx "DEPLOY_COMMIT_SHA=oldsha" "$d/root/deploy.env"; check "tag failure deploy.env intact ($pat)" $?
done
# rollback retag fails -> stop, untag current, drop deploy.env
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"; echo "DEPLOY_COMMIT_SHA=oldsha" >"$d/root/deploy.env"
runcase "$d" STUB_HEALTH=503 STUB_HAS_CURRENT=1 STUB_TAG_FAIL="hermes-agent:previous hermes-agent:current"
[ "$rc" -eq 1 ]; check "rollback tag failure exit 1" $?
has "$d" "systemctl stop hermes"; check "rollback tag failure stops" $?
[ ! -e "$d/root/deploy.env" ]; check "rollback tag failure removes deploy.env" $?

# 5c. rollback has no saved deploy.env -> never write an identity-less env; stop instead
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
runcase "$d" STUB_HEALTH=503 STUB_HAS_CURRENT=1
[ "$rc" -eq 1 ]; check "rollback without saved env exit 1" $?
has "$d" "systemctl stop hermes"; check "rollback without saved env stops" $?
[ ! -e "$d/root/deploy.env" ]; check "no unknown-identity deploy.env written" $?

# 5d. readiness reports a different commit than the one deployed -> rollback
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
echo "DEPLOY_COMMIT_SHA=oldsha" >"$d/root/deploy.env"
echo ghcr.io/loomalpha-labs/hermes-agent:oldold >"$d/root/current_ref"
runcase "$d" STUB_HAS_CURRENT=1 STUB_BODY='{"status":"ready","commit_sha_prefix":"bbbbbbbbbbbb"}'
[ "$rc" -eq 1 ]; check "sha mismatch exit 1" $?
has "$d" "docker tag hermes-agent:previous hermes-agent:current"; check "sha mismatch rolls back" $?
[ "$(cat "$d/root/current_ref")" = "ghcr.io/loomalpha-labs/hermes-agent:oldold" ]; check "sha mismatch keeps current_ref" $?
# ready with no sha field at all is also a failure
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
runcase "$d" STUB_HAS_CURRENT=0 STUB_BODY='{"status":"ready"}'
[ "$rc" -eq 1 ]; check "missing sha in readiness exit 1" $?

# 5e. host-side lock: a second concurrent run refuses without touching anything
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
runcase "$d" STUB_LOCK_FAIL=1
[ "$rc" -eq 2 ]; check "lock held exit 2" $?
[[ "$out" == *"another deploy is running"* ]]; check "lock message" $?
! has "$d" "docker"; check "lock held touches nothing" $?

# 6. bad sha argument
d="$(mkenv)"; touch "$d/root/RAILWAY_STOPPED"
out="$(env PATH="$d/bin:$PATH" STUB_LOG="$d/log" HERMES_ROOT="$d/root" "$HERE/deploy.sh" 'x;rm -rf /' 2>&1)"; rc=$?
[ "$rc" -eq 2 ]; check "bad sha rejected" $?
for bad in abc1234 AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa unknown; do
  out="$(env PATH="$d/bin:$PATH" STUB_LOG="$d/log" HERMES_ROOT="$d/root" "$HERE/deploy.sh" "$bad" 2>&1)"; rc=$?
  [ "$rc" -eq 2 ]; check "sha '$bad' rejected" $?
done

echo "deploy tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
