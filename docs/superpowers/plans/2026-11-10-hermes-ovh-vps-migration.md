# Hermes (Jesse) Railway → OVH VPS Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing Hermes Agent container on an OVH VPS-3 instead of Railway, with no public inbound ports, at about €13/month instead of ~$40.

**Architecture:** The same Docker image runs under systemd on Ubuntu 24.04, with `/srv/hermes/data` bind-mounted at `/data`. A GitHub Actions workflow builds the image on push to `main`, joins the tailnet, and runs `deploy/vps/deploy.sh <sha>` over SSH. That script health-gates the new container and rolls back on failure, then records a GitHub deployment so `request_redeploy.py` keeps working. Telegram uses long polling, so the host accepts no inbound connections except SSH on the Tailscale interface.

**Tech Stack:** Ubuntu 24.04, Docker Engine, systemd, ufw, unattended-upgrades, Tailscale (free tier), GitHub Actions, GHCR (private image).

**Spec:** Owner decisions in the 2026-11-10 desktop session, plus `wiki/docs/hermes-railway-cost-2026-11-10.md` (measured load: memory p99 7.8 GB, max 11.9 GB; CPU 0.78 vCPU; `/data` 39 GB).

## Global Constraints

- Host: OVH VPS-3 (6 vCore, 12 GB RAM, 100 GB NVMe), EU region, Ubuntu 24.04 LTS.
- Swap: 8 GB swapfile, `vm.swappiness=10`.
- Inbound: ufw `default deny incoming`; only `tailscale0` may reach port 22. No other listening port bound to a public address.
- SSH: keys only, `PermitRootLogin no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`.
- Updates: `unattended-upgrades` enabled with automatic reboot at 04:30 UTC (outside the 07:00–10:00 UTC morning job window).
- Secrets live only in `/etc/hermes/hermes.env` (root:root, mode 0600). Never print, log or commit a value. Copy Railway variables machine-to-machine, never through chat.
- Exactly one Telegram poller at any time: Railway must be stopped before the VPS container starts.
- Jesse's schedules, models, providers, budgets and trade gates are unchanged. Signature and hook verification stays enabled.
- No new paid service other than the OVH VPS. Tailscale free tier is the only new dependency.
- Keep the Railway service and volume for 7 days after cutover as rollback; delete only with owner approval.

## Review Focus

1. **Two pollers.** If both the Railway and VPS containers run, Telegram returns 409 conflicts and messages are lost. `deploy.sh` must refuse to start when `/srv/hermes/RAILWAY_STOPPED` is absent (Task 5 creates it at cutover).
2. **Auto-reboot mid-job.** After a 04:30 reboot, the service must come back without a human. Test: `systemctl reboot`, then `/health/ready` returns 200 within 5 minutes.
3. **Tailscale outage locks us out.** The OVH web KVM console is the documented fallback. Test once by logging in through it before cutover.
4. **Disk fills (100 GB).** `check.sh` alerts at 80% used, and Docker image pruning keeps 2 tags.
5. **Morning swap thrash.** Check that peak-hour (08:00–09:30 UTC) jobs finish. Test: on the first two mornings, `vmstat` swap-in stays below 50 MB/s sustained and no job exceeds its timeout.

---

### Task 0 (owner): Order the server

- [ ] Create the OVH account and enable 2FA.
- [ ] Order VPS-3, EU location, Ubuntu 24.04. Paste the public key from `~/.ssh/hermes_vps.pub` at order time.
- [ ] Send the server's public IPv4 address back in chat.
- [ ] Approve `brew install --cask tailscale` on the Mac and log in to Tailscale with 2FA.

### Task 1: Host bootstrap and security check

**Files:**
- Create: `deploy/vps/bootstrap.sh` (idempotent; run once as the initial user)
- Create: `deploy/vps/check.sh` (exit 0 = compliant; prints one line per violation)
- Create: `deploy/vps/hermes-check.service`, `deploy/vps/hermes-check.timer` (weekly, Monday 06:00 UTC; failure posts to Telegram with the bot token from `/etc/hermes/hermes.env`)
- Test: `deploy/vps/test_check.sh` (bats-free; runs `check.sh` against fixture files via `CHECK_ROOT`)

**Interfaces:**
- Produces: user `hermes` (in group `docker`); paths `/srv/hermes/data`, `/etc/hermes/hermes.env`; `check.sh [--root DIR]`.

- [ ] Write `test_check.sh` with three cases:
  - a fixture `sshd_config` containing `PasswordAuthentication yes` → output contains `ssh: password auth enabled`, exit 1;
  - `ufw status` fixture `Status: inactive` → `firewall: inactive`;
  - all-compliant fixtures → exit 0, no output.
- [ ] Run it and expect FAIL (no `check.sh`).
- [ ] Implement `check.sh`. Checks: sshd effective config (the three settings), ufw active + default deny, no non-loopback/non-tailscale listener (`ss -tlnH`), `unattended-upgrades` enabled, swap ≥ 8 GB, `/data` < 80% used, `/var/run/reboot-required` older than 2 days, `hermes.env` mode 0600.
- [ ] Run `test_check.sh` and expect PASS. Run `shellcheck deploy/vps/*.sh` with zero findings.
- [ ] Implement `bootstrap.sh` from the Global Constraints, in this order: apt upgrade → user `hermes` + copy authorized key → Docker Engine (official apt repo) → swapfile → unattended-upgrades → Tailscale `up --ssh=false` → sshd hardening, with `ListenAddress` bound to the Tailscale IP → ufw. Enable ufw last, after confirming Tailscale SSH works, so the session never locks itself out.
- [ ] On the server: run `bootstrap.sh`, then `check.sh` → exit 0. From the Mac, `nc -zv <public-ip> 22` must time out. Log in once through the OVH KVM console (Review Focus 3).
- [ ] Commit: `[claude] vps: host bootstrap and weekly security check`.

### Task 2: Platform-neutral runtime provenance

**Files:**
- Modify: `runtime_readiness.py:180-182, 229-231` (deployment id / commit sha / replica id)
- Modify: `server.py:665` (public domain), `server.py:779` (secure-cookie decision)
- Test: `test_runtime_readiness.py`

**Interfaces:**
- Produces: env contract `DEPLOY_PLATFORM` (`railway|vps`), `DEPLOY_ID`, `DEPLOY_COMMIT_SHA`, `DEPLOY_REPLICA_ID`, `HERMES_PUBLIC_DOMAIN`, `HERMES_SECURE_COOKIES`. Each `DEPLOY_*` value falls back to its `RAILWAY_*` twin, so Railway keeps working until decommission.

- [ ] Add tests:
  - `test_provenance_prefers_deploy_vars`: env `{DEPLOY_COMMIT_SHA: "a"*40, RAILWAY_GIT_COMMIT_SHA: "b"*40}` → `commit_sha == "a"*40`;
  - `test_provenance_falls_back_to_railway`: only `RAILWAY_GIT_COMMIT_SHA` set → that value;
  - `test_secure_cookie_on_vps`: `HERMES_SECURE_COOKIES=1`, http scheme → secure is True.
- [ ] Run and expect the first and third to FAIL.
- [ ] Implement a helper `_deploy_env(env: Mapping[str, str], name: str) -> str | None` in `runtime_readiness.py`, and use it at both sites. In `server.py`, read `HERMES_PUBLIC_DOMAIN` (falling back to `RAILWAY_PUBLIC_DOMAIN`) and `HERMES_SECURE_COOKIES`.
- [ ] Run the full suite (`python -m pytest -q`) and expect all PASS.
- [ ] Commit: `[claude] runtime: platform-neutral deploy provenance`.

### Task 3: Build-and-deploy pipeline with rollback

**Files:**
- Create: `.github/workflows/deploy-vps.yml`
- Create: `deploy/vps/deploy.sh <sha>` (runs on the host as `hermes`)
- Create: `deploy/vps/hermes.service` (systemd unit wrapping `docker run`)
- Modify: `scripts/request_redeploy.py` (receipt matching: accept GitHub deployments in environment `vps-production`)
- Test: `test_request_redeploy.py`, `deploy/vps/test_deploy.sh`

**Interfaces:**
- Consumes: Task 1 paths; Task 2 env contract.
- Produces: image `ghcr.io/loomalpha-labs/hermes-agent:<sha>`; GitHub deployment (environment `vps-production`, status `success`, payload `{"sha": "<sha>"}`) after a passing health gate.

- [ ] Read `request_redeploy.py`'s receipt wait. Add the test `test_accepts_vps_deployment_receipt`: a deployment fixture with `environment: "vps-production"` and a matching sha is accepted, and the Railway-bot creator is not required. Run → FAIL. Implement. Run → PASS.
- [ ] Write `test_deploy.sh` with `docker` stubbed on PATH:
  - health stub returns 503 → script exits 1 and has re-tagged and started the previous image (`hermes-agent:previous`);
  - missing `/srv/hermes/RAILWAY_STOPPED` → exits 2 with `refusing: Railway poller may be running`.
- [ ] Run → FAIL. Implement `deploy.sh`: pull → tag current as `previous` → `systemctl restart hermes` with `DEPLOY_COMMIT_SHA=<sha>` → poll `http://127.0.0.1:${PORT}/health/ready` every 5 s for up to 300 s → on failure, restore `previous` → prune all but 2 tags. Run → PASS.
- [ ] Write the workflow. Trigger: push to `main` and `workflow_dispatch`. Steps: build and push to GHCR; join the tailnet with `tailscale/github-action` (OAuth client secret, tag `tag:ci`); SSH as `hermes` and run `deploy.sh ${{ github.sha }}`; create the GitHub deployment and status. Tailnet ACL: `tag:ci` may reach only `tag:hermes:22`.
- [ ] Commit: `[claude] vps: GHCR build, tailnet deploy with health-gated rollback`.

### Task 4: Data and secrets transfer (pre-cutover, Jesse still live)

**Files:**
- Create: `deploy/vps/transfer.md` (exact commands as run, plus receipts; no secret values)

- [ ] Prune first (owner-approved pattern from 2026-10-30): move superseded `reports/` generations and `backups/` to `/Users/kgm3/Backups/Hermes/2026-11-vps-migration/`, keeping the latest rollback generation on Railway.
- [ ] Pass 1 (live, cold data): stream with `railway ssh … 'tar -C /data --exclude=.hermes/state.db* --exclude=.hermes/state -cf - .' | ssh hermes-vps 'tar -C /srv/hermes/data -xf -'`. Record file count and total bytes on both sides; they must match.
- [ ] Secrets: read the Railway variables through GraphQL and pipe them straight into `ssh hermes-vps 'sudo install -m600 /dev/stdin /etc/hermes/hermes.env'`. Drop `RAILWAY_*` and `PORT`. Add `DEPLOY_PLATFORM=vps` and `HERMES_SECURE_COOKIES=0` (no public HTTP). Verify the same 26 variable *names* exist (names only).
- [ ] Commit `transfer.md` receipts: `[claude] vps: pre-cutover transfer receipts`.

### Task 4b: Off-site encrypted backup (before cutover)

Lesson from the OVH Strasbourg fire (2021): a backup stored with the same provider is not a backup. OVH's included backup stays on, but the real one lives at a second company.

**Files:**
- Create: `deploy/vps/backup.sh`, `deploy/vps/hermes-backup.service`, `deploy/vps/hermes-backup.timer`
- Modify: `deploy/vps/check.sh` (alert if the last successful snapshot is older than 36 h)
- Test: `deploy/vps/test_check.sh` (stale-snapshot case)

- [ ] Owner: create a Cloudflare R2 bucket `hermes-backup` (or Backblaze B2) and a bucket-scoped API key; paste the key into `/etc/hermes/backup.env` over SSH (mode 0600). Claude never sees the value.
- [ ] `backup.sh`: `restic backup /srv/hermes/data` excluding caches, Docker layers and `*.db-wal`; SQLite files copied first with `sqlite3 <db> ".backup <tmp>"` so snapshots are consistent. Retention: `--keep-daily 7 --keep-weekly 4 --keep-monthly 3`, then `restic check --read-data-subset=1%` weekly.
- [ ] Timer: daily 03:30 UTC (before the 04:30 reboot window).
- [ ] Restic repo password: generated on the VPS, stored in `/etc/hermes/backup.env` AND in the owner's password manager (without it the backup is unreadable).
- [ ] Restore drill: restore the latest snapshot to `/tmp/restore-test` and compare `state.db` sha256 and file count. Record in `transfer.md`.
- [ ] Expected cost: ~40 GB → ~$0.25 (B2) to ~$0.60 (R2) per month.
- [ ] Commit: `[claude] vps: nightly off-site restic backup + staleness check`.

### Task 5: Cutover

- [ ] Post in the Jesse Telegram chat: "Moving to new server, back in ~30 min." Wait for no running job executions (`hermes cron list` shows no `running`).
- [ ] Stop Railway: remove the active deployment (the API `deploymentRemove`, as done for LoomAlpha). Confirm `/health/ready` on the Railway URL no longer answers 200.
- [ ] Pass 2 (hot data): copy `.hermes/state.db*` and `.hermes/state/`. sha256 of `state.db` must be identical on both sides.
- [ ] `touch /srv/hermes/RAILWAY_STOPPED`, then run `deploy.sh <current main sha>` → exit 0.
- [ ] Verify:
  - `/health/ready` reports `DEPLOY_COMMIT_SHA` equal to main;
  - the bookmark worker heartbeat is fresh;
  - one `trading-desk-watch-v1` run shows `ok`;
  - the owner sends "ping" and Jesse replies;
  - `check.sh` exits 0.
- [ ] Rollback if any check fails within 2 hours: `systemctl stop hermes` → `rm RAILWAY_STOPPED` → `railway redeploy` the last good deployment. Railway's `/data` is untouched until decommission.
- [ ] Commit the receipts to `deploy/vps/transfer.md`: `[claude] vps: cutover receipts`.

### Task 6: Decommission and docs (cutover + 7 days, owner-approved deletes)

**Files:**
- Modify: `README.md` (deploy section), `wiki/AGENTS.md` and `wiki/docs/jesse-builds.md` (Railway → VPS wording, deploy path), `wiki/docs/hermes-railway-cost-2026-11-10.md` (outcome)

- [ ] Ask the owner, then delete the Railway `Hermes Agent` service, `hermes-agent-volume` and the orphan `strength-tracker-volume`. `project-one-testnet` stays.
- [ ] Update the docs. In the cost note, record actual OVH cost and 7-day memory, swap and CPU on the VPS.
- [ ] Commits: `[claude] docs: Hermes runs on OVH VPS` (template) and `[claude] docs: Jesse VPS cutover outcome` (wiki).
