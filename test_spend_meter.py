import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import spend_meter as sm


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


class BalanceSpend(unittest.TestCase):
    def test_drops_are_spend_rises_are_topups(self):
        snaps = [(ts('2026-11-10T00:00'), 10.0), (ts('2026-11-10T06:00'), 9.0),
                 (ts('2026-11-10T12:00'), 29.0),  # +20 top-up, minus nothing
                 (ts('2026-11-10T23:00'), 27.5)]
        r = sm.balance_spend(snaps, ts('2026-11-10T00:00'), ts('2026-11-11T00:00'))
        self.assertAlmostEqual(r['spent'], 2.5)
        self.assertAlmostEqual(r['topups'], 20.0)
        self.assertAlmostEqual(r['covered_hours'], 23.0)

    def test_window_excludes_outside_snapshots(self):
        snaps = [(ts('2026-11-09T00:00'), 50.0), (ts('2026-11-10T00:00'), 10.0),
                 (ts('2026-11-10T12:00'), 8.0)]
        r = sm.balance_spend(snaps, ts('2026-11-10T00:00'), ts('2026-11-11T00:00'))
        self.assertAlmostEqual(r['spent'], 2.0)

    def test_no_pairs_means_unknown_not_zero(self):
        r = sm.balance_spend([(ts('2026-11-10T00:00'), 10.0)],
                             ts('2026-11-10T00:00'), ts('2026-11-11T00:00'))
        self.assertIsNone(r['spent'])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / 'cron').mkdir()
        (self.home / 'cron/jobs.json').write_text(json.dumps({'jobs': [
            {'id': 'desk', 'name': 'trading-desk-watch-v1', 'deliver': 'telegram:938032749:970884'},
            {'id': 'lab', 'name': 'topic-morning-lab', 'deliver': 'telegram:938032749:972070'},
            {'id': 'tk', 'name': 'tulipking-all-posts', 'deliver': 'telegram:938032749:984806'},
            {'id': 'nm', 'name': 'nautilo', 'deliver': 'telegram:938032749:970892'},
        ]}))
        rows = [
            # in window, Venice
            {'ts': '2026-11-10T10:00:00Z', 'job_id': 'desk', 'prompt_tokens': 1_000_000, 'completion_tokens': 10_000, 'model': 'deepseek-v4-1-flash-1017', 'error': None},
            {'ts': '2026-11-10T11:00:00Z', 'job_id': 'lab', 'prompt_tokens': 300_000, 'completion_tokens': 5_000, 'model': 'deepseek-v4-1-flash-1017', 'error': None},
            {'ts': '2026-11-10T12:00:00Z', 'job_id': 'tk', 'prompt_tokens': 30_000, 'completion_tokens': 200, 'model': 'deepseek-v4-1-flash-1017', 'error': None},
            {'ts': '2026-11-10T12:30:00Z', 'job_id': 'nm', 'prompt_tokens': 10_000, 'completion_tokens': 100, 'model': 'deepseek-v4-1-flash-1017', 'error': None},
            # subscription provider: runs counted, no dollars
            {'ts': '2026-11-10T13:00:00Z', 'job_id': 'desk', 'prompt_tokens': 500_000, 'completion_tokens': 1_000, 'model': 'gpt-6-luna', 'error': None},
            # outside window
            {'ts': '2026-11-09T10:00:00Z', 'job_id': 'tk', 'prompt_tokens': 9_000_000, 'completion_tokens': 0, 'model': 'deepseek-v4-1-flash-1017', 'error': None},
        ]
        (self.home / 'cron/usage_audit.jsonl').write_text(
            ''.join(json.dumps(r) + '\n' for r in rows) + 'not json\n')
        ex = sqlite3.connect(self.home / 'cron/executions.db')
        ex.execute('create table executions (id text primary key, job_id text)')
        ex.executemany('insert into executions values (?,?)',
                       [('e1', 'desk'), ('e2', 'desk'), ('e3', 'lab'), ('e4', 'tk')])
        ex.commit(); ex.close()
        de = sqlite3.connect(self.home / 'cron/deliveries.db')
        de.execute('create table deliveries (execution_id text, status text, created_at text)')
        de.executemany('insert into deliveries values (?,?,?)', [
            ('e1', 'delivered', '2026-11-10T10:00:01+00:00'),
            ('e2', 'delivered', '2026-11-10T10:05:01+00:00'),
            ('e3', 'delivered', '2026-11-10T11:00:01+00:00'),
            ('e4', 'failed', '2026-11-10T12:00:01+00:00'),       # not delivered
            ('gone', 'delivered', '2026-11-10T14:00:00+00:00'),  # no execution row
            ('e1', 'delivered', '2026-11-08T10:00:00+00:00'),    # outside window
        ])
        de.commit(); de.close()
        self.start, self.end = ts('2026-11-10T00:00'), ts('2026-11-11T00:00')

    def tearDown(self):
        self.tmp.cleanup()


class JobShares(Fixture):
    def test_paid_shares_rank_jobs_and_skip_subscription(self):
        r = sm.job_usage(self.home, self.start, self.end)
        names = [n for n, _ in r['paid_shares']]
        self.assertEqual(names[:3], ['trading-desk-watch-v1', 'topic-morning-lab', 'tulipking-all-posts'])
        self.assertAlmostEqual(sum(s for _, s in r['paid_shares']), 1.0)
        self.assertEqual(r['runs_by_provider'], {'venice': 4, 'subscription': 1})
        self.assertGreater(r['paid_estimate_usd'], 0)


class Messages(Fixture):
    def test_counts_delivered_by_job_and_topic(self):
        r = sm.messages_sent(self.home, self.start, self.end)
        self.assertEqual(r['total'], 4)
        self.assertEqual(r['by_job']['trading-desk-watch-v1'], 2)
        self.assertEqual(r['by_job']['unattributed'], 1)
        self.assertEqual(r['by_topic']['Desk'], 2)
        self.assertEqual(r['by_topic']['Lab'], 1)


class Format(unittest.TestCase):
    def test_line_names_dollars_avg_top3_and_messages(self):
        text = sm.format_update({
            'day': {'spent': 3.1234, 'topups': 0.0, 'covered_hours': 24},
            'week': {'spent': 21.0, 'topups': 0.0, 'covered_hours': 168},
            'balance': 5.5,
            'usage': {'paid_shares': [('trading-desk-watch-v1', 0.6), ('topic-morning-lab', 0.25),
                                      ('tulipking-all-posts', 0.1), ('x', 0.05)],
                      'runs_by_provider': {'venice': 40, 'subscription': 2}},
            'messages': {'total': 182, 'by_topic': {'Desk': 154, 'X feed': 16, 'Lab': 3}},
        })
        self.assertIn('$3.12', text)
        self.assertIn('7-day avg $3.00/day', text)
        self.assertIn('~$90/mo', text)
        self.assertIn('trading-desk-watch-v1 60%', text)
        self.assertNotIn(' x 5%', text)
        self.assertIn('182 msgs', text)
        self.assertIn('~1.8 days left', text)
        self.assertLessEqual(len(text.splitlines()), 4)

    def test_partial_coverage_is_labelled_not_extrapolated_silently(self):
        text = sm.format_update({
            'day': {'spent': 1.0, 'topups': 0.0, 'covered_hours': 6},
            'week': {'spent': 1.0, 'topups': 0.0, 'covered_hours': 6},
            'balance': 8.0, 'usage': {'paid_shares': [], 'runs_by_provider': {}},
            'messages': {'total': 0, 'by_topic': {}},
        })
        self.assertIn('meter has 6 h of data', text)

    def test_unknown_spend_says_unknown(self):
        text = sm.format_update({
            'day': {'spent': None, 'topups': 0.0, 'covered_hours': 0},
            'week': {'spent': None, 'topups': 0.0, 'covered_hours': 0},
            'balance': None, 'usage': {'paid_shares': [], 'runs_by_provider': {}},
            'messages': {'total': 0, 'by_topic': {}},
        })
        self.assertIn('spend unknown', text)
        self.assertNotIn('$0.00', text)


class Schedule(unittest.TestCase):
    def test_due_once_after_22_utc(self):
        state = {}
        self.assertFalse(sm.report_due(state, datetime(2026, 11, 11, 21, 59, tzinfo=timezone.utc)))
        self.assertTrue(sm.report_due(state, datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)))
        state['last_report_date'] = '2026-11-11'
        self.assertFalse(sm.report_due(state, datetime(2026, 11, 11, 23, 0, tzinfo=timezone.utc)))
        self.assertTrue(sm.report_due(state, datetime(2026, 11, 12, 22, 5, tzinfo=timezone.utc)))


class Snapshots(unittest.TestCase):
    def test_append_and_load_skip_bad_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'balances.jsonl'
            sm.append_snapshot(p, 100.0, 9.5)
            with p.open('a') as f:
                f.write('garbage\n')
            sm.append_snapshot(p, 200.0, 9.0)
            self.assertEqual(sm.load_snapshots(p), [(100.0, 9.5), (200.0, 9.0)])


if __name__ == '__main__':
    unittest.main()
