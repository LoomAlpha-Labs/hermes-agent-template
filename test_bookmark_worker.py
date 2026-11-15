import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import bookmark_worker as W

class WorkerTests(unittest.TestCase):
    def _git(self, root, *args, check=True):
        return subprocess.run(['git', *args], cwd=root, check=check,
                              capture_output=True, text=True)

    def _state(self, stamp, price):
        return {'latest': {'price_usd': price}, 'last_attempt_utc': stamp,
                'last_good_utc': stamp}

    def _write_state(self, root, value):
        path = root / 'wiki/investing/assets/gocollect-watch/state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + '\n')
        return path

    def _conflicted_repo(self):
        temp = tempfile.TemporaryDirectory(); root = Path(temp.name)
        self._git(root, 'init', '-b', 'master'); self._git(root, 'config', 'user.email', 'test@example.com')
        self._git(root, 'config', 'user.name', 'Test')
        path = self._write_state(root, self._state('2026-09-28T10:00:00Z', 1))
        (root / 'notes.txt').write_text('base\n')
        self._git(root, 'add', '.'); self._git(root, 'commit', '-m', 'base')
        self._git(root, 'checkout', '-b', 'incoming')
        self._write_state(root, self._state('2026-09-28T12:00:00Z', 3))
        self._git(root, 'commit', '-am', 'incoming state')
        self._git(root, 'checkout', 'master')
        self._write_state(root, self._state('2026-09-28T11:00:00Z', 2))
        self._git(root, 'commit', '-am', 'local state')
        self._git(root, 'merge', 'incoming', check=False)
        return temp, root, path

    def test_generated_state_conflict_chooses_freshest_and_preserves_other_work(self):
        temp, root, path = self._conflicted_repo()
        with temp:
            (root / 'notes.txt').write_text('unrelated edit\n')
            (root / 'untracked.txt').write_text('keep me\n')
            self.assertEqual(W.repair_generated_state_conflicts(root),
                             ['wiki/investing/assets/gocollect-watch/state.json'])
            self.assertEqual(json.loads(path.read_text())['latest']['price_usd'], 3)
            self.assertEqual((root / 'notes.txt').read_text(), 'unrelated edit\n')
            self.assertEqual((root / 'untracked.txt').read_text(), 'keep me\n')
            self.assertFalse(self._git(root, 'diff', '--name-only', '--diff-filter=U').stdout)
            self.assertNotIn(path.relative_to(root).as_posix(),
                             self._git(root, 'diff', '--cached', '--name-only').stdout)

    def test_unknown_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); self._git(root, 'init', '-b', 'master')
            self._git(root, 'config', 'user.email', 'test@example.com')
            self._git(root, 'config', 'user.name', 'Test')
            (root / 'human.md').write_text('base\n'); self._git(root, 'add', '.')
            self._git(root, 'commit', '-m', 'base'); self._git(root, 'checkout', '-b', 'other')
            (root / 'human.md').write_text('other\n'); self._git(root, 'commit', '-am', 'other')
            self._git(root, 'checkout', 'master'); (root / 'human.md').write_text('local\n')
            self._git(root, 'commit', '-am', 'local'); self._git(root, 'merge', 'other', check=False)
            with self.assertRaisesRegex(ValueError, 'manual review'):
                W.repair_generated_state_conflicts(root)
            self.assertIn('human.md', self._git(root, 'diff', '--name-only', '--diff-filter=U').stdout)

    def test_sync_retries_once_after_generated_state_repair(self):
        with patch.object(W, 'repair_generated_state_conflicts', return_value=['state.json']) as repair, \
             patch.object(W.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]) as run:
            result = W.sync_repository(Path('/repo'), Path('/live'), {})
            self.assertEqual(result.returncode, 0); repair.assert_called_once()
            self.assertEqual(run.call_count, 2)

    def test_credentials_are_separated(self):
        env={'PATH':'bin','X_BIRD_AUTH_TOKEN':'x-secret','X_BIRD_CT0':'csrf-secret', 'X_OAUTH_CLIENT_SECRET':'oauth-secret',
             'X_BOOKMARK_OAUTH_BOOTSTRAP':'token-secret',
             'TELEGRAM_BOT_TOKEN':'telegram','OPENROUTER_API_KEY':'model','MANIFEST_HMAC_KEY':'sign'}
        self.assertEqual(W.bird_env(env), {'PATH':'bin','AUTH_TOKEN':'x-secret','CT0':'csrf-secret'})
        self.assertFalse(any(k.startswith(('X_BIRD_', 'X_OAUTH_')) for k in W.jesse_env(env)))
        self.assertNotIn('X_BOOKMARK_OAUTH_BOOTSTRAP', W.jesse_env(env))
        with self.assertRaises(ValueError): W.bird_env({})

    def test_fixed_bird_command_and_stale_oauth_setting(self):
        env = {'PATH': '/bin', 'X_BIRD_AUTH_TOKEN': 'cookie-one',
               'X_BIRD_CT0': 'cookie-two', 'X_BOOKMARK_PROVIDER': 'oauth',
               'X_OAUTH_CLIENT_SECRET': 'oauth-secret'}
        posts = [{'id': '123', 'text': 'bookmark', 'author': {'username': 'alice'}}]
        with patch.object(W.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(posts).encode())) as run:
            self.assertEqual(W.collect_bird(env), posts)
        run.assert_called_once_with(
            ['bird', 'bookmarks', '--count', '100', '--json', '--plain'],
            env={'PATH': '/bin', 'AUTH_TOKEN': 'cookie-one', 'CT0': 'cookie-two'},
            capture_output=True, timeout=90)

    def test_bird_rejects_partial_or_malformed_output(self):
        valid = {'id': '123', 'text': 'bookmark', 'author': {'username': 'alice'}}
        bad_responses = [[], {}, [valid, valid], [valid] * 101,
                         [{'id': '', 'text': 'x', 'author': {'username': 'a'}}],
                         [{'id': 123, 'text': 'x', 'author': {'username': 'a'}}],
                         [{'id': '123', 'author': {'username': 'a'}}],
                         [{'id': '123', 'text': 'x', 'author': {}}],
                         [valid, None]]
        with patch.object(W, 'bird_env', return_value={}), patch.object(W.subprocess, 'run') as run:
            for response in bad_responses:
                with self.subTest(response=response):
                    run.return_value = subprocess.CompletedProcess([], 0, json.dumps(response).encode())
                    with self.assertRaisesRegex(ValueError, 'Invalid bookmark response'):
                        W.collect_bird({})
            for payload in (b'not json', b'[{"id":', b'\xff'):
                with self.subTest(payload=payload):
                    run.return_value = subprocess.CompletedProcess([], 0, payload)
                    with self.assertRaisesRegex(ValueError, 'Invalid bookmark response'):
                        W.collect_bird({})

    def test_bird_timeout_and_nonzero_never_publish_stderr(self):
        with patch.object(W, 'bird_env', return_value={}), patch.object(W.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 1, b'secret stdout', b'secret stderr')
            with self.assertRaisesRegex(ValueError, '^Bookmark read failed$') as error:
                W.collect_bird({})
            self.assertNotIn('secret', str(error.exception))
            run.side_effect = subprocess.TimeoutExpired(['bird'], 90, stderr=b'secret stderr')
            with self.assertRaises(subprocess.TimeoutExpired):
                W.collect_bird({})

    def test_successful_cycle_writes_snapshot_without_oauth(self):
        posts = [{'id': '123', 'text': 'bookmark', 'author': {'username': 'alice'}}]
        with tempfile.TemporaryDirectory() as td, \
             patch.object(W, 'sync_repository', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(W, 'verify_runtime'), patch.object(W, 'collect_bird', return_value=posts) as bird, \
             patch.dict('sys.modules', {'bookmark_oauth': None}), \
             contextlib.redirect_stdout(io.StringIO()):
            path = Path(td) / 'status.json'; state = {}
            W.cycle(state, path, 'a' * 12, clock=lambda: 100)
            self.assertEqual(json.loads((Path(td) / 'snapshot.json').read_text())['posts'], posts)
            self.assertEqual(state['collection_status'], 'ok')
            self.assertEqual(state['next_run_at'], 3700)
            bird.assert_called_once_with()

    def test_signature_failure_prevents_bird_and_preserves_deadline(self):
        with tempfile.TemporaryDirectory() as td, patch.object(W,'job_state'),patch.object(W,'verify_runtime',side_effect=ValueError('bad signature')),patch.object(W.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run,contextlib.redirect_stdout(io.StringIO()):
            path=Path(td)/'status.json'; state={}
            W.cycle(state,path,'a'*12,clock=lambda:100)
            self.assertEqual(run.call_count,1)
            self.assertEqual(state['next_run_at'],3700); self.assertEqual(state['status'],'failed')

    def test_failed_read_suppresses_secret_stderr_in_snapshot_and_status(self):
        with tempfile.TemporaryDirectory() as td,patch.object(W,'job_state'),patch.object(W,'verify_runtime'),patch.object(W,'bird_env',return_value={}),patch.object(W.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0),subprocess.CompletedProcess([],1,b'',b'secret cookie')]),contextlib.redirect_stdout(io.StringIO()):
            path=Path(td)/'status.json'; state={}; W.cycle(state,path,'a'*12,clock=lambda:100)
            self.assertEqual(json.loads((Path(td)/'snapshot.json').read_text())['status'],'failed')
            self.assertNotIn('secret cookie',path.read_text())
            self.assertNotIn('secret cookie',(Path(td)/'snapshot.json').read_text())

    def test_delta_fixed_command_cookies_and_event_filter(self):
        env = {'PATH': '/bin', 'X_BIRD_AUTH_TOKEN': 'secret-a', 'X_BIRD_CT0': 'secret-b',
               'X_OAUTH_CLIENT_SECRET': 'unused', 'OPENROUTER_API_KEY': 'unused'}
        tweets = [
            {'id': '101', 'text': 'Jensen Huang board clearance confirmed',
             'author': {'username': 'DeltaOne'}, 'extra': {'secret': 'omit'}},
            {'id': '102', 'text': 'Jensen Huang board restored',
             'author': {'username': 'other'}},
            {'id': '103', 'text': 'market opens higher', 'author': {'username': 'DeltaOne'}},
            {'id': '104', 'text': 'Huang returns to board', 'author': {'username': 'DELTAONE'}}]
        with patch.object(W.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({'tweets': tweets, 'nextCursor': 'cursor'}).encode())) as run:
            self.assertEqual(W.collect_delta_tweets(env), tweets)
        run.assert_called_once_with(
            ['bird', 'user-tweets', '@DeltaOne', '-n', '50', '--json', '--plain'],
            env={'PATH': '/bin', 'AUTH_TOKEN': 'secret-a', 'CT0': 'secret-b'},
            capture_output=True, timeout=90)
        selected = W.delta_event_tweets(tweets)
        self.assertEqual([post['id'] for post in selected], ['101', '104'])
        self.assertEqual(selected[0]['url'], 'https://x.com/DeltaOne/status/101')
        self.assertNotIn('extra', selected[0])

    def test_delta_rejects_malformed_partial_empty_or_error(self):
        valid = {'id': '101', 'text': 'Jensen Huang board cleared',
                 'author': {'username': 'DeltaOne'}}
        bad = [[], [valid, valid], [valid] * 51,
               [{'id': 101, 'text': 'x', 'author': {'username': 'DeltaOne'}}],
               [{'id': '1', 'text': 'x', 'author': {'username': 'other'}}],
               [{'id': '1', 'text': 'x', 'author': {}}], [None]]
        with patch.object(W, 'bird_env', return_value={}), patch.object(W.subprocess, 'run') as run:
            for tweets in bad:
                with self.subTest(tweets=tweets):
                    run.return_value = subprocess.CompletedProcess(
                        [], 0, json.dumps({'tweets': tweets}).encode())
                    with self.assertRaisesRegex(ValueError, 'Invalid DeltaOne response'):
                        W.collect_delta_tweets({})
            for payload in (b'[]', b'{"tweets":', b'\xff'):
                run.return_value = subprocess.CompletedProcess([], 0, payload)
                with self.assertRaisesRegex(ValueError, 'Invalid DeltaOne response'):
                    W.collect_delta_tweets({})
            run.return_value = subprocess.CompletedProcess([], 1, b'secret stdout', b'secret stderr')
            with self.assertRaisesRegex(ValueError, '^DeltaOne read failed$'):
                W.collect_delta_tweets({})

    def test_delta_cycle_private_atomic_and_separate_deadline(self):
        tweets = [{'id': '101', 'text': 'Jensen Huang board restored',
                   'author': {'username': 'DeltaOne'}}]
        with tempfile.TemporaryDirectory() as td, patch.object(W, 'collect_delta_tweets', return_value=tweets):
            state_dir = Path(td) / 'delta'
            state = W.delta_cycle(state_dir, clock=lambda: 100)
            snapshot = state_dir / 'snapshot.json'
            self.assertEqual(json.loads(snapshot.read_text())['posts'][0]['id'], '101')
            self.assertEqual(state['next_run_at'], 1000)
            self.assertEqual(state['collection_status'], 'ok')
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(state_dir.glob('.snapshot-*')))
            self.assertFalse((state_dir / 'gate.json').exists())
            self.assertFalse((state_dir / 'delivered.json').exists())
            with patch.object(W, 'collect_delta_tweets', side_effect=subprocess.TimeoutExpired(
                    ['bird'], 90, stderr=b'secret cookie')):
                failed = W.delta_cycle(state_dir, clock=lambda: 1000)
            data = snapshot.read_text()
            self.assertEqual(failed['collection_status'], 'failed')
            self.assertEqual(json.loads(data)['status'], 'failed')
            self.assertNotIn('posts', json.loads(data))
            self.assertNotIn('secret cookie', data)
            self.assertEqual(failed['next_run_at'], 1900)

    def test_delta_state_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); private = root / 'outside'; private.mkdir()
            (root / 'delta').symlink_to(private, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'not private'):
                W.private_atomic(root / 'delta' / 'snapshot.json', {'status': 'ok'})
            self.assertFalse((private / 'snapshot.json').exists())

    def test_failed_delivery_does_not_acknowledge(self):
        with tempfile.TemporaryDirectory() as td,patch.object(W,'job_state',return_value={'last_run_at':'2026-09-27T15:00:00+00:00','last_status':'ok','last_delivery_error':'failed'}):
            with self.assertRaises(ValueError): W.acknowledge('a'*12,100,Path(td))
            self.assertFalse((Path(td)/'delivered.json').exists())

    def test_silent_success_cannot_acknowledge_an_expected_message(self):
        with tempfile.TemporaryDirectory() as td:
            state=Path(td); W.atomic(state/'gate.json',{'gate_run_at':100,'wakeAgent':True,'status':'activation'})
            with patch.object(W,'job_state',return_value={'last_run_at':'2026-09-27T15:00:00+00:00','last_status':'ok'}),patch.object(W,'delivery_confirmed',return_value=False):
                with self.assertRaises(ValueError): W.acknowledge('a'*12,100,state)
            self.assertFalse((state/'delivered.json').exists())

    def test_unpublished_completion_remains_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); state=root/'state'; state.mkdir()
            W.atomic(state/'gate.json',{'gate_run_at':100,'wakeAgent':False,'status':'new','batch':'projects/x-bookmark-review/batches/test.json'})
            receipt=root/'projects/x-bookmark-review/completed/test.json'; receipt.parent.mkdir(parents=True); receipt.write_text('{}')
            with patch.object(W,'ROOT',root),patch.object(W,'job_state',return_value={'last_run_at':'2026-09-27T15:00:00+00:00','last_status':'ok'}),patch.object(W.subprocess,'run',return_value=subprocess.CompletedProcess([],1,b'')):
                with self.assertRaises(ValueError): W.acknowledge('a'*12,100,state)
            self.assertFalse((state/'delivered.json').exists())

if __name__=='__main__': unittest.main()
