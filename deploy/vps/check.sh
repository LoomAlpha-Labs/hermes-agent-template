#!/usr/bin/env bash
# Hermes host security check. Exit 0 = compliant; otherwise one line per
# violation on stdout and exit 1. Prints no secret values.
#
# Usage: check.sh [--root DIR]     (or CHECK_ROOT=DIR)
#
# With a root override, values come from fixture files in DIR:
#   sshd_effective          output of `sshd -T` (lowercase "key value" lines)
#   ufw_status              output of `ufw status verbose`
#   ss_tlnH                 output of `ss -tlnH`
#   uu_enabled              output of `systemctl is-enabled unattended-upgrades`
#   swap_bytes              total swap in bytes
#   data_used_pct           percent used of /srv/hermes/data (integer)
#   reboot_required_age_days  age in whole days of /var/run/reboot-required
#                           (file absent = no reboot pending)
#   hermes_env_mode         octal mode of /etc/hermes/hermes.env, e.g. 600
#   hermes_env_owner        owner:group of /etc/hermes/hermes.env, e.g. root:root
# Without an override, the same files are gathered live from the host.
set -u

ROOT="${CHECK_ROOT:-}"
if [ "${1:-}" = "--root" ]; then ROOT="${2:-}"; fi

DATA_DIR=/srv/hermes/data
ENV_FILE=/etc/hermes/hermes.env
# 8 GiB swapfile reports 8388604 kB (mkswap header page); allow one 64 KiB page of slack.
MIN_SWAP=$((8 * 1024 * 1024 * 1024 - 65536))

if [ -z "$ROOT" ]; then
  ROOT="$(mktemp -d)"
  trap 'rm -rf "$ROOT"' EXIT
  sshd -T >"$ROOT/sshd_effective" 2>/dev/null
  ufw status verbose >"$ROOT/ufw_status" 2>/dev/null
  ss -tlnH >"$ROOT/ss_tlnH" 2>/dev/null
  systemctl is-enabled unattended-upgrades >"$ROOT/uu_enabled" 2>/dev/null
  awk 'NR>1 {s+=$3} END {print s*1024+0}' /proc/swaps >"$ROOT/swap_bytes"
  df --output=pcent "$DATA_DIR" 2>/dev/null | tail -1 | tr -dc '0-9' >"$ROOT/data_used_pct"
  if [ -e /var/run/reboot-required ]; then
    echo $(( ( $(date +%s) - $(stat -c %Y /var/run/reboot-required) ) / 86400 )) >"$ROOT/reboot_required_age_days"
  fi
  stat -c %a "$ENV_FILE" >"$ROOT/hermes_env_mode" 2>/dev/null
  stat -c %U:%G "$ENV_FILE" >"$ROOT/hermes_env_owner" 2>/dev/null
fi

# True if any ALLOW/LIMIT rule without "on tailscale0" names port 22, a list or
# range containing 22, the OpenSSH profile, or "Anywhere" (all ports).
ufw_covers_22_publicly() {
  local line to tok lo hi
  while IFS= read -r line; do
    case "$line" in *ALLOW*|*LIMIT*) ;; *) continue ;; esac
    to="$(sed -E 's/[[:space:]]+(ALLOW|LIMIT)[[:space:]].*//' <<<"$line")"
    case "$to" in *" on tailscale0"*) continue ;; esac
    to="${to%% on *}"; to="${to// (v6)/}"
    case "$to" in OpenSSH|Anywhere) return 0 ;; esac
    to="${to%%/*}"
    IFS=, read -ra toks <<<"$to"
    for tok in "${toks[@]}"; do
      lo="${tok%%:*}"; hi="${tok##*:}"
      if [ "$lo" -le 22 ] 2>/dev/null && [ "$hi" -ge 22 ] 2>/dev/null; then return 0; fi
    done
  done <"$1"
  return 1
}

violations=()
v() { violations+=("$1"); }
have() { [ -s "$ROOT/$1" ]; }

# --- ssh ---
if have sshd_effective; then
  grep -qix 'passwordauthentication no' "$ROOT/sshd_effective" || v "ssh: password auth enabled"
  grep -qix 'permitrootlogin no' "$ROOT/sshd_effective" || v "ssh: root login permitted"
  grep -qix 'kbdinteractiveauthentication no' "$ROOT/sshd_effective" || v "ssh: keyboard-interactive auth enabled"
else
  v "ssh: no effective config available"
fi

# --- firewall ---
fw_ok=0
if have ufw_status; then
  if ! grep -q '^Status: active' "$ROOT/ufw_status"; then
    v "firewall: inactive"
  else
    fw_ok=1
    if ! grep -q '^Default: deny (incoming)' "$ROOT/ufw_status"; then
      v "firewall: default incoming policy is not deny"; fw_ok=0
    fi
    # Any ALLOW/LIMIT rule not scoped to tailscale0 that covers port 22 breaks the SSH gate.
    if ufw_covers_22_publicly "$ROOT/ufw_status"; then
      v "firewall: port 22 allowed on a non-tailscale0 interface"; fw_ok=0
    fi
  fi
else
  v "firewall: status unavailable"
fi

# --- listeners ---
# sshd on port 22 is exempt only when ufw is active, default deny, and 22 is tailscale0-only.
if have ss_tlnH; then
  while read -r _ _ _ local _; do
    [ -n "$local" ] || continue
    port="${local##*:}"; addr="${local%:*}"; addr="${addr#[}"; addr="${addr%]}"; addr="${addr%%%*}"
    case "$addr" in
      127.*|::1) continue ;;
      fd7a:*) continue ;;
      100.*)
        o2="${addr#100.}"; o2="${o2%%.*}"
        if [ "$o2" -ge 64 ] 2>/dev/null && [ "$o2" -le 127 ]; then continue; fi ;;
    esac
    if [ "$port" = "22" ] && [ "$fw_ok" = 1 ]; then continue; fi
    v "listener: $local is bound to a public address"
  done <"$ROOT/ss_tlnH"
fi

# --- updates ---
if [ "$(tr -d '[:space:]' <"$ROOT/uu_enabled" 2>/dev/null)" != "enabled" ]; then
  v "updates: unattended-upgrades not enabled"
fi

# --- swap ---
swap="$(tr -dc '0-9' <"$ROOT/swap_bytes" 2>/dev/null)"
if [ "${swap:-0}" -lt "$MIN_SWAP" ]; then
  v "swap: ${swap:-0} bytes, need at least $MIN_SWAP (8 GB)"
fi

# --- disk ---
pct="$(tr -dc '0-9' <"$ROOT/data_used_pct" 2>/dev/null)"
if [ -z "$pct" ]; then v "disk: $DATA_DIR usage unavailable"
elif [ "$pct" -ge 80 ]; then v "disk: $DATA_DIR ${pct}% used"; fi

# --- pending reboot ---
if have reboot_required_age_days; then
  age="$(tr -dc '0-9' <"$ROOT/reboot_required_age_days")"
  if [ "${age:-0}" -gt 2 ]; then v "reboot: pending for ${age} days"; fi
fi

# --- secrets file ---
mode="$(tr -dc '0-9' <"$ROOT/hermes_env_mode" 2>/dev/null)"
if [ "$mode" != "600" ]; then
  v "secrets: $ENV_FILE mode ${mode:-missing}, want 600"
fi
owner="$(tr -d '[:space:]' <"$ROOT/hermes_env_owner" 2>/dev/null)"
if [ "$owner" != "root:root" ]; then
  v "secrets: $ENV_FILE owner ${owner:-unknown}, want root:root"
fi

if [ "${#violations[@]}" -gt 0 ]; then
  printf '%s\n' "${violations[@]}"
  exit 1
fi
exit 0
