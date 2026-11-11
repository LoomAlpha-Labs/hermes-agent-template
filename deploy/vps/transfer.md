# Railway → VPS transfer receipts

Server: Hostinger KVM 4 `srv2148922` (France), public 2.25.2.116, tailnet 100.115.22.83 (`tag:hermes`).
No secret values appear in this file.

## Pass 1 — live copy, Jesse still on Railway (2026-11-11)

- Route: direct server-to-server. Railway container streamed `tar -C /data . | gzip -1 | openssl enc -aes-256-ctr -pbkdf2`
  over raw TCP to a one-shot receiver on VPS port 47900. ufw allowed only the Railway egress IP
  (152.55.176.243) for the duration. The one-time key was shredded on both ends and the Mac afterwards;
  the ufw rule was deleted and nothing listens on 47900.
- Why not `railway ssh`: ~2 MB/s with base64, not 8-bit clean (raw bytes corrupted), and the websocket
  dropped after ~1 minute.
- Time: 00:45:58 → 01:06:22 UTC (~20 min). Compressed+encrypted stream: 20,018,102,062 bytes.
- Exit codes: receiver `python 0, openssl 0, gzip -d 0, tar 0`. Sender `tar 1` (expected: "file changed
  as we read it" on live `state.db`, `state.db-wal`, `state/`), final socket recv raised a harmless
  BlockingIOError after shutdown; the gzip CRC check on the receiver passed, so the stream was complete.
- Counts (regular files, excluding lost+found): Railway 328,497 files / ~38.08 GB; VPS 328,498 files /
  38,078,696,677 bytes. The one-file difference is live churn; pass 2 re-copies hot data at cutover.
- Nothing on Railway was modified or deleted. Planned pruning to the Mac archive was skipped: the VPS has
  183 GB free, so everything was copied.
- `check.sh` after the transfer: exit 0.

## Secrets

- `/etc/hermes/hermes.env` (root:root 0600, 1,521 bytes, 27 names) built from Railway service variables via
  GraphQL and piped straight to `sudo install -m 600 /dev/stdin` on the VPS. Values never printed.
- Kept 24 Railway names; dropped `PORT`, 14 `RAILWAY_*` names and `WUFUN_RPC_URL` (null on Railway).
- Added `DEPLOY_PLATFORM=vps`, `HERMES_SECURE_COOKIES=0`, `HERMES_ALERT_CHAT_ID` (owner Telegram chat).

## Pass 2 — at cutover (2026-11-11)

- No cron job was running. Jesse was frozen in place on Railway with SIGSTOP (server, start.sh, bookmark
  worker, dashboard, gateway) at 01:18:42Z, so the container stayed up for the copy but nothing polled
  Telegram or wrote files.
- Same direct encrypted route as pass 1, with a fresh one-time key. Railway egress IP was still 152.55.176.243.
- Set: every regular file newer than 2026-11-11T00:45:58Z, every `*.db`, `*.db-wal`, `*.db-shm`, and all of
  `.hermes/state/` — 250,560 files, 10,585,140,675 bytes sent = received. Exit codes 0 0 0 0 on both ends.
- sha256 identical on both sides for `state.db`, `state.db-wal`, `state.db-shm`, `state.db.fts_rebuild.lock`.
- Afterwards: ufw rule deleted, nothing listens on 47900, key shredded on both ends.
- Known gap: files deleted on Railway after pass 1 started still exist on the VPS (pass 2 only adds/overwrites).

## Cutover (2026-11-11)

- Railway autodeploy disconnected first (service source repo = none); empty commit 434ddd2 to main created no
  Railway deployment. Branch merged to main as e07ce9b.
- Railway deployment 9a37c6e8 removed via `deploymentRemove` at ~01:29Z (status REMOVED). Volume untouched.
- `/srv/hermes/RAILWAY_STOPPED` created 01:30Z; `VPS_DEPLOY_ENABLED=true`; deploy-vps run 42939409765 green (8m59s).
- Verify: `/health/ready` ready, commit e07ce9bc5134 = main; one gateway + one bookmark worker in the container;
  `trading-desk-watch-v1` last run 01:39:19Z `ok`; `check.sh` exit 0. Telegram ping: owner confirmed Jesse replied.
