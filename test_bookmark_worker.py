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
    def test_credentials_are_separated(self):
        env={'PATH':'bin','X_BIRD_AUTH_TOKEN':'x-secret','X_BIRD_CT0':'csrf-secret',
             'TELEGRAM_BOT_TOKEN':'telegram','OPENROUTER_API_KEY':'model','MANIFEST_HMAC_KEY':'sign'}
        self.assertEqual(W.bird_env(env), {'PATH':'bin','AUTH_TOKEN':'x-secret','CT0':'csrf-secret'})
        self.assertFalse(any(k.startswith('X_BIRD_') for k in W.jesse_env(env)))
        with self.assertRaises(ValueError): W.bird_env({})

    def test_signature_failure_prevents_bird_and_preserves_deadline(self):
        with tempfile.TemporaryDirectory() as td, patch.object(W,'job_state'),patch.object(W,'verify_runtime',side_effect=ValueError('bad signature')),patch.object(W.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run,patch.object(W,'dispatch') as dispatch,contextlib.redirect_stdout(io.StringIO()):
            path=Path(td)/'status.json'; state={}
            W.cycle(state,path,'a'*12,clock=lambda:100)
            self.assertEqual(run.call_count,1); dispatch.assert_not_called()
            self.assertEqual(state['next_run_at'],3700); self.assertEqual(state['status'],'failed')

    def test_failed_read_suppresses_secret_stderr_but_runs_failure_gate(self):
        with tempfile.TemporaryDirectory() as td,patch.object(W,'job_state'),patch.object(W,'verify_runtime'),patch.object(W,'bird_env',return_value={}),patch.object(W.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0),subprocess.CompletedProcess([],1,b'',b'secret cookie')]),patch.object(W,'dispatch') as dispatch,patch.object(W,'acknowledge',return_value='blocked'),contextlib.redirect_stdout(io.StringIO()):
            path=Path(td)/'status.json'; state={}; W.cycle(state,path,'a'*12,clock=lambda:100)
            self.assertEqual(json.loads((Path(td)/'snapshot.json').read_text())['status'],'failed')
            self.assertNotIn('secret cookie',path.read_text()); dispatch.assert_called_once()

    def test_failed_delivery_does_not_acknowledge(self):
        with tempfile.TemporaryDirectory() as td,patch.object(W,'job_state',return_value={'last_run_at':'2026-09-27T15:00:00+00:00','last_status':'ok','last_delivery_error':'failed'}):
            with self.assertRaises(ValueError): W.acknowledge('a'*12,100,Path(td))
            self.assertFalse((Path(td)/'delivered.json').exists())

    def test_unpublished_completion_remains_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); state=root/'state'; state.mkdir()
            W.atomic(state/'gate.json',{'status':'new','batch':'projects/x-bookmark-review/batches/test.json'})
            receipt=root/'projects/x-bookmark-review/completed/test.json'; receipt.parent.mkdir(parents=True); receipt.write_text('{}')
            with patch.object(W,'ROOT',root),patch.object(W,'job_state',return_value={'last_run_at':'2026-09-27T15:00:00+00:00','last_status':'ok'}),patch.object(W.subprocess,'run',return_value=subprocess.CompletedProcess([],1,b'')):
                with self.assertRaises(ValueError): W.acknowledge('a'*12,100,state)
            self.assertFalse((state/'delivered.json').exists())

if __name__=='__main__': unittest.main()
