"""Owner-enabled JEV worker; separate from Hermes's sandboxed cron scripts.

The signed collector owns the shared $10 ledger. Only its existing OpenRouter
credential is forwarded; no gateway, Telegram, exchange or signing credentials.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/data/.hermes/workspace/wiki')
LIVE = Path('/data/.hermes/scripts')
STATE = Path('/data/.hermes/state/jev-live')
INTERVAL = 1800


def write_status(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def collector_env(environ):
    allowed = ('PATH', 'HOME', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
               'OPENROUTER_API_KEY', 'JEV_SHADOW_DISABLED')
    return {key: environ[key] for key in allowed if key in environ}


def verify_runtime():
    result = subprocess.run([sys.executable, str(LIVE / 'manifest_tool.py'),
        'verify', '--dir', str(ROOT / 'agent/scripts')],
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('signed runtime verification failed')
    approved = set(result.stdout.splitlines())
    for name in ('jev_live_watch.py', 'repo_paths.py'):
        source, deployed = ROOT / 'agent/scripts' / name, LIVE / name
        if name not in approved or deployed.is_symlink():
            raise ValueError('unapproved runtime')
        if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(deployed.read_bytes()).digest():
            raise ValueError('runtime hash mismatch')


def cycle(state, status_path, verify=verify_runtime, clock=time.time):
    # Persist the next deadline BEFORE execution. A restart never fast-retries a
    # paid attempt; the collector also retains uncertain request reservations.
    started = clock()
    state.update(status='running', started_at=started, next_run_at=started + INTERVAL)
    write_status(status_path, state)
    try:
        verify()
        result = subprocess.run([sys.executable, str(LIVE / 'jev_live_watch.py')],
            cwd=ROOT, env=collector_env(os.environ), capture_output=True, text=True, timeout=330)
        state.update(status='waiting' if result.returncode == 0 else 'failed',
                     last_exit_code=result.returncode)
        state.pop('error_type', None)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        state.update(status='failed', last_exit_code=3, error_type=type(exc).__name__)
    state['completed_at'] = clock()
    write_status(status_path, state)
    # Source-level details remain on the private persistent volume for review.
    print(json.dumps({'worker': 'jev', 'status': state['status'],
        'exit_code': state['last_exit_code'],
        'next_run_utc': datetime.fromtimestamp(state['next_run_at'], timezone.utc).isoformat()}), flush=True)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        status_path = STATE / 'worker-status.json'
        state = json.loads(status_path.read_text()) if status_path.exists() else {}
        state.update(pid=os.getpid(), worker_started_at=time.time(), interval_seconds=INTERVAL)
        while True:
            if time.time() >= state.get('next_run_at', 0):
                cycle(state, status_path)
            state['heartbeat_at'] = time.time()
            write_status(status_path, state)
            time.sleep(15)


if __name__ == '__main__':
    main()
