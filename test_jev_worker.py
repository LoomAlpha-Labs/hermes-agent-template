import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import jev_worker as W


class WorkerTests(unittest.TestCase):
    def test_collector_gets_only_its_credential(self):
        env = W.collector_env({'OPENROUTER_API_KEY':'provider', 'TELEGRAM_BOT_TOKEN':'private',
            'MANIFEST_HMAC_KEY':'signing', 'GITHUB_TOKEN':'git', 'PATH':'bin'})
        self.assertEqual(env, {'OPENROUTER_API_KEY':'provider', 'PATH':'bin'})

    def test_deadline_survives_failure_and_secret_stderr_is_not_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'status.json'
            state = {}
            def failed(*args, **kwargs):
                self.assertEqual(json.loads(path.read_text())['next_run_at'], 1900)
                raise subprocess.TimeoutExpired(args[0], 330, stderr='private credential')
            with patch.object(W.subprocess, 'run', side_effect=failed) as run, contextlib.redirect_stdout(io.StringIO()) as out:
                W.cycle(state, path, verify=lambda: None, clock=lambda:100)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(state['status'], 'failed')
            self.assertEqual(state['next_run_at'], 1900)
            self.assertNotIn('private credential', path.read_text()+out.getvalue())

    def test_bad_signature_prevents_collector_execution(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(W.subprocess, 'run') as run:
            def invalid(): raise ValueError('invalid signature')
            with contextlib.redirect_stdout(io.StringIO()):
                W.cycle({}, Path(tmp)/'status.json', verify=invalid, clock=lambda:100)
            run.assert_not_called()

    def test_success_schedules_next_tick_without_retry_loop(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(W.subprocess, 'run',
                return_value=subprocess.CompletedProcess([],0,'{}')) as run:
            state={}
            with contextlib.redirect_stdout(io.StringIO()):
                W.cycle(state, Path(tmp)/'status.json', verify=lambda:None, clock=lambda:100)
            self.assertEqual(state['status'],'waiting')
            self.assertEqual(state['next_run_at'],1900)
            self.assertEqual(run.call_count,1)


if __name__ == '__main__': unittest.main()
