#!/usr/bin/env python3
"""Fail the image build if a real plugin sync loses template dependencies."""
import json
from pathlib import Path
import subprocess
import sys

from pm.environments import committed_venv, selected_venv, venv_python
from pm.install import _runtime_state_matches
from pm.lock import Facts
from pm.packages import Venv
from pm.paths import facts_path

root = Path('/opt/hermes-agent')
selected = selected_venv(root)
expected_venv = root / '.venv'
if selected.resolve() != expected_venv.resolve() or committed_venv(root) is not None:
    raise RuntimeError('Plugin transaction selected a replacement application generation')
extras = ['all', 'messaging', 'tts-premium', 'bedrock', 'anthropic', 'edge-tts', 'vision']
fact = Facts(facts_path()).get('venv') or {}
expected_stamp = Venv(root).expected_stamp(sorted(extras), plugin_dirs=[])
if not _runtime_state_matches(fact, expected_stamp):
    raise RuntimeError('Packaged baseline selection is not current')
probe = '''
import importlib.metadata as m, io, json, wave
expected = {'honcho-ai': '2.2.0', 'hindsight-client': '0.6.1',
            'yt-dlp': '2026.8.19', 'yt-dlp-ejs': '0.8.0',
            'faster-whisper': '1.2.1', 'ctranslate2': '4.8.2',
            'onnxruntime': '1.30.0', 'av': '18.0.0'}
for name, version in expected.items():
    assert m.version(name) == version, (name, m.version(name))
import run_agent, gateway.run, cron.scheduler, ctranslate2, yt_dlp
from faster_whisper.audio import decode_audio
b = io.BytesIO()
w = wave.open(b, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
w.writeframes(bytes(32000)); w.close(); b.seek(0)
assert len(decode_audio(b, sampling_rate=16000)) == 16000
assert 'int8' in ctranslate2.get_supported_compute_types('cpu')
print(json.dumps({'packages_verified': sorted(expected)}))
'''
for executable in (Path(sys.executable), venv_python(selected)):
    subprocess.run([str(executable), '-c', probe], cwd=root, check=True, timeout=60)
print('Shipped and PM-selected application capabilities passed')
