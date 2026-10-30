#!/usr/bin/env python3
"""Verify resident PM helpers with temporary fixtures; never acquire tools."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave

import pm
from pm.build_operations import verified_tools
from pm.paths import store_root
from pm.store import current_target
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations
from hermes_cli.web_routers.audio import _ffmpeg_s16le_mono

root = Path('/opt/hermes-agent')
store = root / 'tools'
names = ('node', 'npm', 'ffmpeg', 'ripgrep')
if store_root().resolve() != store.resolve():
    raise RuntimeError('Active PM store is not the image-owned store')
verified = verified_tools(names, source_store=store, target=current_target())
for name in names:
    entry = verified.entries[name]
    installed = pm.installed_package(name)
    if not installed or not entry.binary or installed.binary.resolve() != entry.binary.resolve():
        raise RuntimeError(f'{name} is not available from the verified store')
    if not entry.binary.resolve().is_relative_to(store.resolve()):
        raise RuntimeError(f'{name} escaped the image store')
# Changes only this process's PATH; does not install or touch configuration.
problems = pm.activate()
if any(name in problem for name in names for problem in problems):
    raise RuntimeError('PM activation reports a required helper out of sync')
# Gateway CLI runs pm.activate at startup; verify its resulting PATH, not
# the original SSH process environment or /proc's startup environment.
for name, package in (('node', 'node'), ('npm', 'npm'), ('rg', 'ripgrep'), ('ffmpeg', 'ffmpeg')):
    actual = shutil.which(name)
    if not actual or Path(actual).resolve() != verified.entries[package].binary.resolve():
        raise RuntimeError(f'Activated PATH does not select managed {name}')
ffprobe_on_path = shutil.which('ffprobe')
if not ffprobe_on_path or not Path(ffprobe_on_path).resolve().is_relative_to(store.resolve()):
    raise RuntimeError('Activated PATH does not select managed ffprobe')
env = pm.env_for('npm', base_env=dict(os.environ))
with tempfile.TemporaryDirectory(prefix='hermes-npm-cache-') as npm_cache:
    env['npm_config_cache'] = npm_cache
    env['npm_config_update_notifier'] = 'false'
    for name in ('node', 'npm', 'npx'):
        binary = shutil.which(name, path=env.get('PATH', ''))
        if not binary or not Path(binary).resolve().is_relative_to(store.resolve()):
            raise RuntimeError(f'{name} does not resolve inside managed store')
        subprocess.run([binary, '--version'], env=env, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20)
ffmpeg = verified.entries['ffmpeg'].binary
ffprobe = ffmpeg.with_name('ffprobe')
if not ffprobe.is_file() or not ffprobe.resolve().is_relative_to(store.resolve()):
    raise RuntimeError('Managed ffprobe unavailable')
with tempfile.TemporaryDirectory(prefix='hermes-helpers-') as folder:
    base = Path(folder)
    valid, invalid, sample = base / 'valid.js', base / 'invalid.js', base / 'sample.txt'
    valid.write_text('const x = 3;\n', encoding='utf-8')
    invalid.write_text('const = ;\n', encoding='utf-8')
    sample.write_text('helper-fixture-8724\n', encoding='utf-8')
    shell = LocalEnvironment(cwd=folder, timeout=15, persistent=False)
    ops = ShellFileOperations(shell, cwd=folder)
    ok, bad = ops._check_lint(str(valid)), ops._check_lint(str(invalid))
    if ok.skipped or not ok.success or bad.skipped or bad.success:
        raise RuntimeError('Native JavaScript lint did not accept valid and reject invalid input')
    resolved = ops._resolve_command('rg')
    if not resolved or Path(resolved).resolve() != verified.entries['ripgrep'].binary.resolve():
        raise RuntimeError('Native search did not choose managed ripgrep')
    found = ops.search('helper-fixture-8724', path=folder, target='content')
    if found.error or not any('helper-fixture-8724' in match.content for match in found.matches):
        raise RuntimeError('Native search fixture failed')
    shell.cleanup()
    wav, encoded = base / 'sound.wav', base / 'sound.flac'
    with wave.open(str(wav), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000); out.writeframes(bytes(4800))
    subprocess.run([str(ffmpeg), '-v', 'error', '-i', str(wav), str(encoded)], check=True, timeout=20)
    subprocess.run([str(ffprobe), '-v', 'error', '-show_entries', 'stream=codec_name',
                    '-of', 'default=nw=1', str(encoded)], check=True, stdout=subprocess.DEVNULL, timeout=20)
    pcm, rate = _ffmpeg_s16le_mono(str(encoded))
    if not pcm or rate != 24000:
        raise RuntimeError('Native audio decode failed')
result = {'result': 'passed', 'versions': {name: verified.entries[name].version for name in names},
          'checks': ['activated-store-first-path', 'managed-paths', 'node-npm-npx', 'js-lint-valid-invalid', 'native-rg-search',
                     'ffmpeg-ffprobe-native-decode'], 'empty_home': '--empty-home' in sys.argv}
if '--empty-home' not in sys.argv:
    with tempfile.TemporaryDirectory(prefix='hermes-helper-home-') as home:
        child_env = dict(os.environ, HOME=home, HERMES_HOME=str(Path(home) / '.hermes'))
        # A different temporary default home must still read the stamped store.
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--empty-home'],
                               env=child_env, check=True, capture_output=True, text=True, timeout=120)
        nested = json.loads(child.stdout.strip().splitlines()[-1])
        if nested.get('result') != 'passed' or not nested.get('empty_home'):
            raise RuntimeError('Empty-home verification failed')
        result['checks'].append('empty-home-store')
print(json.dumps(result, sort_keys=True))
