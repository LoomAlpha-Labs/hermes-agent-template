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

## Pass 2 — at cutover (pending)

Re-copy with Jesse stopped on Railway: SQLite files (`*.db`, `*-wal`, `*-shm`) and every file newer than
pass 1 start (2026-11-11T00:45:58Z), same direct route.
