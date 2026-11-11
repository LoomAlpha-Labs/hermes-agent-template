"""Jesse spend meter: real Venice dollars from balance snapshots, plus a job split.

Dollars come from the Venice account balance (read with the inference key Jesse
already has; no admin key). Spend over a window = the sum of balance drops
between consecutive snapshots; a rise is a top-up, never negative spend.

The job split and message counts come from files Hermes already writes:
cron/usage_audit.jsonl (tokens per agent run), cron/deliveries.db and
cron/executions.db (Telegram deliveries). They rank jobs; they are not invoices.

Usage (inside the container):
  python spend_meter.py snapshot          # record one balance reading
  python spend_meter.py report [--send]   # print (or send) the daily update
  python spend_meter.py worker            # hourly snapshot + 22:00 UTC update
Read-only toward Hermes state. Writes only under state/spend-meter/.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get('HERMES_HOME', '/data/.hermes'))
STATE_DIR = HOME / 'state' / 'spend-meter'
VENICE_RATE_LIMITS = 'https://api.venice.ai/api/v1/api_keys/rate_limits'
UPDATE_TO = 'telegram:938032749:970888'   # Agent Ops / maintenance topic
REPORT_HOUR_UTC = 22                      # after the US close
DAY, WEEK = 86400, 7 * 86400

# Venice list prices, USD per 1M tokens (checked 2026-11-11 via /api/v1/models).
# Used only to weight the job split. CACHED_SHARE is the share of prompt tokens
# served from cache (measured ~90% on Jesse's runs, 2026-11-03..11-10).
VENICE_PRICES = {'deepseek-v4-1-flash-1017': (0.30, 0.0075, 1.20),
                 'gemini-3-5-flash-lite': (0.375, 0.0375, 3.125)}
DEFAULT_PRICE = (0.30, 0.0075, 1.20)
CACHED_SHARE = 0.9
SUBSCRIPTION_PREFIXES = ('gpt-',)         # openai-codex: flat subscription

TOPICS = {'970882': 'Jesse', '970884': 'Desk', '970886': 'Research',
          '970888': 'Agent Ops', '970890': 'Personal', '970892': 'Nautilo',
          '972070': 'Lab', '984806': 'X feed'}


def _ts(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


# ---------- balance snapshots ----------

def append_snapshot(path, when, usd):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps({'ts': when, 'usd': usd}) + '\n')


def load_snapshots(path):
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            out.append((float(row['ts']), float(row['usd'])))
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(out)


def balance_spend(snaps, start, end):
    inside = [s for s in snaps if start <= s[0] <= end]
    if len(inside) < 2:
        return {'spent': None, 'topups': 0.0, 'covered_hours': 0}
    spent = topups = 0.0
    for (_, a), (_, b) in zip(inside, inside[1:]):
        if b < a:
            spent += a - b
        else:
            topups += b - a
    return {'spent': spent, 'topups': topups,
            'covered_hours': round((inside[-1][0] - inside[0][0]) / 3600, 1)}


def read_venice_usd(key, timeout=20):
    req = urllib.request.Request(VENICE_RATE_LIMITS,
                                 headers={'Authorization': f'Bearer {key}'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    data = data.get('data', data)
    return float(data['balances']['USD'])


# ---------- job split and messages ----------

def _jobs(home):
    try:
        return {j['id']: j for j in json.loads((home / 'cron/jobs.json').read_text())['jobs']}
    except (OSError, ValueError, KeyError):
        return {}


def job_usage(home, start, end):
    jobs = _jobs(home)
    cost = defaultdict(float)
    runs = Counter()
    path = home / 'cron/usage_audit.jsonl'
    lines = path.read_text().splitlines() if path.exists() else []
    for line in lines:
        try:
            r = json.loads(line)
            when = _ts(r['ts'])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not start <= when < end:
            continue
        model = r.get('model') or ''
        if model.startswith(SUBSCRIPTION_PREFIXES):
            runs['subscription'] += 1
            continue
        runs['venice'] += 1
        p_in, p_cache, p_out = VENICE_PRICES.get(model, DEFAULT_PRICE)
        prompt = r.get('prompt_tokens') or 0
        out = r.get('completion_tokens') or 0
        usd = (prompt * ((1 - CACHED_SHARE) * p_in + CACHED_SHARE * p_cache) + out * p_out) / 1e6
        cost[jobs.get(r.get('job_id'), {}).get('name', r.get('job_id') or '?')] += usd
    total = sum(cost.values())
    shares = sorted(((k, v / total) for k, v in cost.items()), key=lambda kv: -kv[1]) if total else []
    return {'paid_shares': shares, 'paid_estimate_usd': total, 'runs_by_provider': dict(runs)}


def messages_sent(home, start, end):
    jobs = _jobs(home)
    by_job, by_topic = Counter(), Counter()
    db = home / 'cron/deliveries.db'
    if not db.exists():
        return {'total': 0, 'by_job': {}, 'by_topic': {}}
    lo = datetime.fromtimestamp(start, timezone.utc).isoformat()
    hi = datetime.fromtimestamp(end, timezone.utc).isoformat()
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        con.execute('attach database ? as e', (f'file:{home / "cron/executions.db"}?mode=ro',))
        rows = con.execute(
            "select x.job_id from deliveries d left join e.executions x on x.id = d.execution_id "
            "where d.status = 'delivered' and d.created_at >= ? and d.created_at < ?", (lo, hi)).fetchall()
    finally:
        con.close()
    for (job_id,) in rows:
        job = jobs.get(job_id)
        if not job:
            by_job['unattributed'] += 1
            by_topic['unattributed'] += 1
            continue
        by_job[job['name']] += 1
        thread = str(job.get('deliver') or '').split(':')[-1]
        by_topic[TOPICS.get(thread, thread or 'local')] += 1
    return {'total': len(rows), 'by_job': dict(by_job), 'by_topic': dict(by_topic)}


# ---------- the daily line ----------

def _usd(v):
    return f'${v:.2f}'


def format_update(r):
    day, week, bal = r['day'], r['week'], r['balance']
    if day['spent'] is None:
        head = '💸 Jesse spend unknown (not enough Venice balance readings yet)'
    else:
        head = f"💸 Jesse spent {_usd(day['spent'])} on Venice in the last 24 h"
        if week['spent'] is not None and week['covered_hours']:
            per_day = week['spent'] / (week['covered_hours'] / 24)
            head += f" · 7-day avg {_usd(per_day)}/day (~${per_day * 30:.0f}/mo)"
            if bal is not None and per_day > 0:
                head += f" · balance {_usd(bal)}, ~{bal / per_day:.1f} days left"
        if week['covered_hours'] < 24 * 7:
            head += f" (meter has {week['covered_hours']:g} h of data)"
    if day.get('topups'):
        head += f" · top-up {_usd(day['topups'])}"
    lines = [head]
    shares = r['usage']['paid_shares'][:3]
    if shares:
        lines.append('Top jobs: ' + ', '.join(f'{n} {s * 100:.0f}%' for n, s in shares))
    sub = r['usage']['runs_by_provider'].get('subscription')
    if sub:
        lines.append(f'Codex subscription (flat fee) runs: {sub}')
    m = r['messages']
    topics = sorted(m['by_topic'].items(), key=lambda kv: -kv[1])[:4]
    lines.append(f"{m['total']} msgs" + (' — ' + ', '.join(f'{k} {v}' for k, v in topics) if topics else ''))
    return '\n'.join(lines)


def build_report(home, now):
    snaps = load_snapshots(home / 'state/spend-meter/balances.jsonl')
    return {
        'generated_at': now,
        'day': balance_spend(snaps, now - DAY, now),
        'week': balance_spend(snaps, now - WEEK, now),
        'balance': snaps[-1][1] if snaps else None,
        'usage': job_usage(home, now - DAY, now),
        'messages': messages_sent(home, now - DAY, now),
    }


def report_due(state, now_dt):
    return now_dt.hour >= REPORT_HOUR_UTC and state.get('last_report_date') != now_dt.date().isoformat()


def send(text):
    # Jesse's own sender, as in brain_refresh.sh; no bot token handled here.
    subprocess.run(['hermes', 'send', '--quiet', '--to', UPDATE_TO], input=text, text=True,
                   check=True, timeout=60)


# ---------- entry points ----------

def take_snapshot(home):
    key = os.environ.get('CUSTOM_PROVIDER_API_KEY')
    if not key:
        raise RuntimeError('CUSTOM_PROVIDER_API_KEY not set')
    usd = read_venice_usd(key)
    append_snapshot(home / 'state/spend-meter/balances.jsonl', time.time(), usd)
    return usd


def save_report(home, report):
    out = home / 'state/spend-meter/reports.jsonl'
    out.parent.mkdir(parents=True, exist_ok=True)
    row = dict(report, usage=dict(report['usage'], paid_shares=report['usage']['paid_shares'][:10]))
    with out.open('a') as f:
        f.write(json.dumps(row) + '\n')


def worker(home):
    state_path = home / 'state/spend-meter/worker-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    while True:
        try:
            take_snapshot(home)
        except Exception as exc:  # network or key problem: log the type, never the key
            print(json.dumps({'worker': 'spend-meter', 'snapshot_error': type(exc).__name__}), flush=True)
        now_dt = datetime.now(timezone.utc)
        if report_due(state, now_dt):
            try:
                report = build_report(home, now_dt.timestamp())
                send(format_update(report))
                save_report(home, report)
                state['last_report_date'] = now_dt.date().isoformat()
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(state))
            except Exception as exc:
                print(json.dumps({'worker': 'spend-meter', 'report_error': type(exc).__name__}), flush=True)
        time.sleep(3600 - time.time() % 3600 + 5)   # next hour mark


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('cmd', choices=['snapshot', 'report', 'worker'])
    ap.add_argument('--send', action='store_true', help='send the report to Telegram')
    ap.add_argument('--json', action='store_true', help='print the raw report')
    a = ap.parse_args(argv)
    if a.cmd == 'snapshot':
        print(f'venice balance {_usd(take_snapshot(HOME))}')
    elif a.cmd == 'report':
        report = build_report(HOME, time.time())
        text = format_update(report)
        print(json.dumps(report, indent=1) if a.json else text)
        if a.send:
            send(text)
    else:
        worker(HOME)
    return 0


if __name__ == '__main__':
    sys.exit(main())
