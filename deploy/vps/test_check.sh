#!/usr/bin/env bash
# Tests for check.sh using fixture directories (works on macOS, no root needed).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0

mkroot() {  # build a fully compliant fixture root
  local d; d="$(mktemp -d)"
  printf 'permitrootlogin no\npasswordauthentication no\nkbdinteractiveauthentication no\nport 22\n' >"$d/sshd_effective"
  cat >"$d/ufw_status" <<'U'
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp on tailscale0       ALLOW IN    Anywhere
22/tcp (v6) on tailscale0  ALLOW IN    Anywhere (v6)
U
  cat >"$d/ss_tlnH" <<'S'
LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*
LISTEN 0 128 127.0.0.53%lo:53 0.0.0.0:*
LISTEN 0 128 100.101.102.103:41641 0.0.0.0:*
LISTEN 0 128 [::1]:631 [::]:*
LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 128 [::]:22 [::]:*
S
  echo enabled >"$d/uu_enabled"
  echo 8589934592 >"$d/swap_bytes"
  echo 42 >"$d/data_used_pct"
  echo 600 >"$d/hermes_env_mode"
  echo root:root >"$d/hermes_env_owner"
  echo "$d"
}

run() {  # run <name> <expected_exit> <expected_substring|""> <root>
  local name=$1 want=$2 sub=$3 root=$4 out rc
  out="$("$HERE/check.sh" --root "$root" 2>&1)"; rc=$?
  if [ "$rc" -ne "$want" ]; then echo "FAIL $name: exit $rc, want $want"; echo "$out"; fail=$((fail+1)); return; fi
  if [ -n "$sub" ] && ! grep -qF -- "$sub" <<<"$out"; then echo "FAIL $name: missing '$sub'"; echo "$out"; fail=$((fail+1)); return; fi
  if [ -z "$sub" ] && [ -n "$out" ]; then echo "FAIL $name: expected no output"; echo "$out"; fail=$((fail+1)); return; fi
  echo "ok   $name"; pass=$((pass+1))
}

r=$(mkroot); run "all compliant" 0 "" "$r"
r=$(mkroot); sed -i.bak 's/passwordauthentication no/passwordauthentication yes/' "$r/sshd_effective"
run "password auth" 1 "ssh: password auth enabled" "$r"
r=$(mkroot); sed -i.bak 's/permitrootlogin no/permitrootlogin yes/' "$r/sshd_effective"
run "root login" 1 "ssh: root login permitted" "$r"
r=$(mkroot); sed -i.bak 's/kbdinteractiveauthentication no/kbdinteractiveauthentication yes/' "$r/sshd_effective"
run "kbd-interactive" 1 "ssh: keyboard-interactive auth enabled" "$r"
r=$(mkroot); echo "Status: inactive" >"$r/ufw_status"
run "ufw inactive" 1 "firewall: inactive" "$r"
r=$(mkroot); sed -i.bak 's/deny (incoming)/allow (incoming)/' "$r/ufw_status"
run "ufw default allow" 1 "firewall: default incoming policy is not deny" "$r"
r=$(mkroot); echo 'LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*' >>"$r/ss_tlnH"
run "public 8080" 1 "8080" "$r"
r=$(mkroot); echo '22/tcp                     ALLOW IN    Anywhere' >>"$r/ufw_status"
run "ssh open on any iface" 1 "firewall: port 22 allowed on a non-tailscale0 interface" "$r"
r=$(mkroot); echo '22/tcp                     ALLOW IN    Anywhere' >>"$r/ufw_status"
run "22 listener flagged when ufw non-compliant" 1 "listener: 0.0.0.0:22" "$r"
r=$(mkroot); echo 'OpenSSH                    ALLOW IN    Anywhere' >>"$r/ufw_status"
run "leftover OpenSSH rule" 1 "firewall: port 22 allowed on a non-tailscale0 interface" "$r"
r=$(mkroot); echo '20:30/tcp                  ALLOW IN    Anywhere' >>"$r/ufw_status"
run "range containing 22" 1 "firewall: port 22 allowed on a non-tailscale0 interface" "$r"
r=$(mkroot); echo '22,80/tcp                  ALLOW IN    Anywhere' >>"$r/ufw_status"
run "list containing 22" 1 "firewall: port 22 allowed on a non-tailscale0 interface" "$r"
r=$(mkroot); echo '80,443/tcp                 ALLOW IN    Anywhere' >>"$r/ufw_status"
run "unrelated public ports rule ok" 0 "" "$r"
r=$(mkroot); echo enabled >"$r/uu_enabled"; echo disabled >"$r/uu_enabled"
run "unattended-upgrades off" 1 "updates: unattended-upgrades not enabled" "$r"
r=$(mkroot); echo 8589930496 >"$r/swap_bytes"
run "8G swapfile as reported by kernel" 0 "" "$r"
r=$(mkroot); echo 4294967296 >"$r/swap_bytes"
run "swap small" 1 "swap:" "$r"
r=$(mkroot); echo 85 >"$r/data_used_pct"
run "disk full" 1 "disk: /srv/hermes/data 85% used" "$r"
r=$(mkroot); echo 3 >"$r/reboot_required_age_days"
run "reboot pending" 1 "reboot: pending for 3 days" "$r"
r=$(mkroot); echo 1 >"$r/reboot_required_age_days"
run "reboot pending recent ok" 0 "" "$r"
r=$(mkroot); echo 644 >"$r/hermes_env_mode"
run "env mode" 1 "secrets: /etc/hermes/hermes.env mode 644, want 600" "$r"
r=$(mkroot); echo ubuntu:root >"$r/hermes_env_owner"
run "env owner" 1 "owner ubuntu:root, want root:root" "$r"
r=$(mkroot); echo root:hermes >"$r/hermes_env_owner"
run "env group" 1 "owner root:hermes, want root:root" "$r"

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
