#!/usr/bin/env bash
# Hermes VPS bootstrap (Ubuntu 24.04). Run once as the initial sudo user:
#   sudo ./bootstrap.sh
# Idempotent: safe to re-run. Order matters: ufw is enabled last, only after
# Tailscale is confirmed up, so the session cannot lock itself out.
# Prints no secret values.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo -E bash "$0" "$@"
HERE="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive
INIT_USER="${SUDO_USER:-}"

log() { echo "==> $*"; }
die() { echo "ABORT: $*" >&2; exit 1; }

# 1. System update
log "apt upgrade"
apt-get update -y
apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg ufw unattended-upgrades

# Reboot time below is local time; pin to UTC so 04:30 means 04:30 UTC.
timedatectl set-timezone UTC

# 2. User hermes + authorized key
log "user hermes"
id hermes >/dev/null 2>&1 || adduser --disabled-password --gecos "" hermes
install -d -m 700 -o hermes -g hermes /home/hermes/.ssh
if [ -n "$INIT_USER" ] && [ -s "/home/$INIT_USER/.ssh/authorized_keys" ]; then
  src="/home/$INIT_USER/.ssh/authorized_keys"
elif [ -s /root/.ssh/authorized_keys ]; then
  src=/root/.ssh/authorized_keys
else
  src=""
fi
if [ -n "$src" ]; then
  touch /home/hermes/.ssh/authorized_keys
  while IFS= read -r key; do
    [ -n "$key" ] && ! grep -qxF "$key" /home/hermes/.ssh/authorized_keys && echo "$key" >>/home/hermes/.ssh/authorized_keys
  done <"$src"
fi
chown hermes:hermes /home/hermes/.ssh/authorized_keys 2>/dev/null || true
chmod 600 /home/hermes/.ssh/authorized_keys 2>/dev/null || true
[ -s /home/hermes/.ssh/authorized_keys ] || die "hermes has no authorized SSH key; refusing to disable password auth"

# 3. Docker Engine (official apt repo)
log "docker"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
usermod -aG docker hermes
# Note: Docker-published ports bypass ufw. Compose must bind to 127.0.0.1 (or publish nothing).

# Data + secrets paths
install -d -m 750 -o hermes -g hermes /srv/hermes/data
install -d -m 700 -o root -g root /etc/hermes
touch /etc/hermes/hermes.env
chown root:root /etc/hermes/hermes.env
chmod 600 /etc/hermes/hermes.env
# Add HERMES_ALERT_CHAT_ID=<telegram chat id> to hermes.env for weekly-check alerts.

# 4. Swap: 8 GB, swappiness 10
log "swap"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  [ -e /swapfile ] || { fallocate -l 8G /swapfile; chmod 600 /swapfile; mkswap /swapfile; }
  swapon /swapfile
fi
grep -qE '^/swapfile[[:space:]]' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
echo 'vm.swappiness=10' >/etc/sysctl.d/99-hermes.conf
sysctl -w vm.swappiness=10 >/dev/null

# 5. unattended-upgrades with automatic reboot at 04:30 UTC
log "unattended-upgrades"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'C'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
C
cat >/etc/apt/apt.conf.d/52hermes-unattended <<'C'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
C
systemctl enable --now unattended-upgrades

# 6. Tailscale (interactive login unless already up)
log "tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
  apt-get update -y
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled
tailscale_up() { tailscale status --json 2>/dev/null | grep -q '"BackendState": *"Running"'; }
if ! tailscale_up; then
  echo "Tailscale login required; follow the URL printed below."
  tailscale up --ssh=false --advertise-tags=tag:hermes
fi
tailscale_up || die "Tailscale is not up and logged in; not enabling the firewall (would risk lockout)"
TS_IP="$(tailscale ip -4 | head -1)"
[ -n "$TS_IP" ] || die "no Tailscale IPv4 address"
log "tailscale up at $TS_IP"

# 7. sshd hardening (drop-in; 00- sorts before cloud-init's 50- file, first value wins).
# No ListenAddress: sshd binds all addresses and ufw is the gate, which avoids
# a boot-order lockout if sshd starts before tailscale0 exists.
log "sshd"
cat >/etc/ssh/sshd_config.d/00-hermes.conf <<'C'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
C
sshd -t || die "sshd config invalid"
systemctl reload ssh

# 8. Weekly check timer
log "check timer"
install -d /usr/local/lib/hermes
install -m 755 "$HERE/check.sh" "$HERE/check-and-alert.sh" /usr/local/lib/hermes/
install -m 644 "$HERE/hermes-check.service" "$HERE/hermes-check.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hermes-check.timer

# 8b. Hermes container unit + scoped sudo for deploy.sh (runs as hermes)
log "hermes service + sudoers"
chown hermes:hermes /srv/hermes  # deploy.sh writes deploy.env, current_ref here
install -m 644 "$HERE/hermes.service" /etc/systemd/system/hermes.service
systemctl daemon-reload
systemctl enable hermes  # not started: first deploy.sh run starts it
tmp_sudoers="$(mktemp)"
install -m 440 "$HERE/hermes-sudoers" "$tmp_sudoers"
visudo -cf "$tmp_sudoers" >/dev/null || { rm -f "$tmp_sudoers"; die "hermes-sudoers failed visudo validation"; }
install -m 440 -o root -g root "$tmp_sudoers" /etc/sudoers.d/hermes
rm -f "$tmp_sudoers"

# 9. Firewall, last: only tailscale0 may reach port 22
log "ufw"
ufw default deny incoming
ufw default allow outgoing
ufw delete allow 22/tcp >/dev/null 2>&1 || true
ufw delete allow OpenSSH >/dev/null 2>&1 || true
ufw allow in on tailscale0 to any port 22 proto tcp
ufw --force enable
log "done. Verify: ssh hermes@$TS_IP over Tailscale, then sudo $HERE/check.sh"
