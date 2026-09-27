"""Hourly owner-authorized Bird worker; Jesse receives data, never X credentials."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

ROOT = Path('/data/.hermes/workspace/wiki')
LIVE = Path('/data/.hermes/scripts')
STATE = Path('/data/.hermes/state/x-bookmark-review')
INTERVAL = 3600


def atomic(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def bird_env(environ):
    allowed = ('PATH', 'HOME', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
    result = {k: environ[k] for k in allowed if k in environ}
    for source, target in [('X_BIRD_AUTH_TOKEN', 'AUTH_TOKEN'), ('X_BIRD_CT0', 'CT0')]:
        if not environ.get(source):
            raise ValueError('Bookmark session is unavailable')
        result[target] = environ[source]
    return result


def jesse_env(environ):
    return {k: v for k, v in environ.items()
            if k not in ('AUTH_TOKEN', 'CT0') and not k.startswith('X_BIRD_')}


def verify_runtime():
    result = subprocess.run([sys.executable, str(LIVE / 'manifest_tool.py'),
        'verify', '--dir', str(ROOT / 'agent/scripts')], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Signed runtime verification failed')
    approved = set(result.stdout.decode().splitlines())
    for name in ('x_bookmark_watch.py', 'repo_paths.py'):
        source, deployed = ROOT / 'agent/scripts' / name, LIVE / name
        if name not in approved or deployed.is_symlink():
            raise ValueError('Unapproved bookmark runtime')
        if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(deployed.read_bytes()).digest():
            raise ValueError('Bookmark runtime hash mismatch')


def job_state(job_id):
    data = load(Path('/data/.hermes/cron/jobs.json'), {})
    rows = data if isinstance(data, list) else data.get('jobs', [])
    match = [j for j in rows if j.get('id') == job_id]
    if len(match) != 1 or match[0].get('name') != 'hourly-x-bookmark-research':
        raise ValueError('Bookmark review job is missing')
    if not match[0].get('enabled'):
        raise ValueError('Bookmark research schedule is paused')
    return match[0]


def delivery_confirmed(job_id, since):
    with sqlite3.connect('file:/data/.hermes/cron/executions.db?mode=ro', uri=True) as db:
        rows = db.execute('SELECT status, delivery_outcome, started_at FROM executions WHERE job_id=? ORDER BY claimed_at DESC LIMIT 3', (job_id,)).fetchall()
    return any(status == 'completed' and outcome == 'delivered' and at and
               datetime.fromisoformat(at).timestamp() >= since - 5
               for status, outcome, at in rows)


def acknowledge(job_id, started, state_dir=STATE):
    job = job_state(job_id)
    at = datetime.fromisoformat(job.get('last_run_at') or '1970-01-01T00:00:00+00:00').timestamp()
    if at < started - 5 or job.get('last_status') != 'ok' or job.get('last_delivery_error'):
        raise ValueError('Jesse completion or Telegram delivery is unconfirmed')
    gate = load(state_dir / 'gate.json', {})
    if at < gate.get('gate_run_at', float('inf')) - 5:
        raise ValueError('Current review has not completed')
    if gate.get('wakeAgent') and not delivery_confirmed(job_id, gate['gate_run_at']):
        raise ValueError('Telegram delivery receipt is missing; retain report')
    done = load(state_dir / 'delivered.json', {'receipts': [], 'activated': False})
    targets = list(gate.get('deliver_receipts', []))
    if gate.get('batch'):
        targets.append(Path(gate['batch']).stem)
    if gate.get('status') == 'awaiting_publish':
        targets.extend(Path(p).stem for p in gate.get('receipts', []))
    for batch_id in targets:
        path = ROOT / 'projects/x-bookmark-review/completed' / (batch_id + '.json')
        if not path.exists():
            raise ValueError('Review did not finish its batch; keep pending')
        result = subprocess.run(['git', 'show', 'origin/main:' + path.relative_to(ROOT).as_posix()],
            cwd=ROOT, capture_output=True, timeout=20)
        if result.returncode or result.stdout != path.read_bytes():
            raise ValueError('Completion receipt is not published; keep pending')
    done['receipts'] = sorted(set(done['receipts']) | set(targets))
    if gate.get('status') == 'activation':
        done['activated'] = True
    if gate.get('status') == 'blocked' and gate.get('wakeAgent'):
        gate['failure_notified'] = gate['reason']
        atomic(state_dir / 'gate.json', gate)
    done['last_success_at'] = time.time()
    done['last_ack_job_at'] = job['last_run_at']
    atomic(state_dir / 'delivered.json', done)
    return gate.get('status', 'unknown')


def cycle(state, status_path, job_id, clock=time.time):
    started = clock()
    state.update(status='running', started_at=started, next_run_at=started + INTERVAL)
    atomic(status_path, state)
    try:
        sync = subprocess.run(['bash', str(LIVE / 'wiki_git_pull.sh')],
            cwd=ROOT, env=jesse_env(os.environ), capture_output=True, timeout=120)
        verify_runtime()
        snapshot = {'collected_at_utc': datetime.now(timezone.utc).isoformat(),
                    'sync_ok': sync.returncode == 0, 'status': 'failed'}
        try:
            bird = subprocess.run(['bird', 'bookmarks', '--count', '100', '--json', '--plain'],
                env=bird_env(os.environ), capture_output=True, timeout=90)
            if bird.returncode:
                raise ValueError('Bookmark read failed')
            posts = json.loads(bird.stdout)
            if not isinstance(posts, list) or not 1 <= len(posts) <= 100:
                raise ValueError('Invalid bookmark response')
            snapshot.update(status='ok', posts=posts)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # Never save/log raw stderr, response fragments or credentials.
            snapshot['status'] = 'failed'
        atomic(status_path.parent / 'snapshot.json', snapshot)
        state.update(status='waiting',
                     collection_status=snapshot['status'], sync_ok=snapshot['sync_ok'])
        state.pop('error', None)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        state.update(status='failed', error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    state['completed_at'] = clock()
    atomic(status_path, state)
    print(json.dumps({'worker': 'bookmarks', 'status': state['status'],
                      'next_run_at': state['next_run_at']}), flush=True)


def main():
    job_id = os.environ.get('X_BOOKMARK_REVIEW_JOB_ID', '')
    if not re.fullmatch(r'[a-f0-9]{12}', job_id):
        raise ValueError('Set the existing bookmark review job ID')
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        path = STATE / 'worker-status.json'
        state = load(path, {})
        state.update(pid=os.getpid(), worker_started_at=time.time(), interval_seconds=INTERVAL)
        while True:
            if time.time() >= state.get('next_run_at', 0):
                cycle(state, path, job_id)
            # Observe scheduler completion without dispatching or waking a model.
            # Collection and research each have one owner; the cron stays hourly.
            try:
                job = job_state(job_id)
                done = load(STATE / 'delivered.json', {})
                if job.get('last_run_at') and job.get('last_run_at') != done.get('last_ack_job_at') and not job.get('fire_claim'):
                    gate = load(STATE / 'gate.json', {})
                    acknowledge(job_id, gate.get('gate_run_at', float('inf')))
                    state.pop('review_error', None)
            except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                state['review_error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            state['heartbeat_at'] = time.time()
            atomic(path, state)
            time.sleep(15)


if __name__ == '__main__':
    main()
