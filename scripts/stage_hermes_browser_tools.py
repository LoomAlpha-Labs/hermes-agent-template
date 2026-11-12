#!/usr/bin/env python3
"""Prepare the upstream-locked Chromium into the image-owned PM store.

v0.23 removed agent-browser: browser_exec drives Chromium over CDP, so Chromium is
the only browser tool left in pm/lock.json.

In Docker, Hermes never lazy-installs Chromium ("Docker supplies the binary"), so
without this the browser tool is absent after every deploy. Run after the apt
layer that installs Chromium's shared libraries: the linkage check below fails
the build if any library is still missing.
"""
import json
from pathlib import Path
import subprocess

import pm
from pm.build_operations import verified_tools
from pm.lock import Facts
from pm.store import current_target

store = Path('/opt/hermes-agent/tools')
names = ['chromium']
before = Facts(store / 'facts.json', strict=True).get('venv')
if not before:
    raise RuntimeError('Missing packaged application venv selection')
pm.prepare_tools(names, out=store, target=current_target())
after = Facts(store / 'facts.json', strict=True).get('venv')
if before != after:
    raise RuntimeError('Preparing browser tools changed the packaged venv selection')
verified = verified_tools(names, source_store=store, target=current_target())
for name in names:
    item = verified.entries[name]
    if item.binary is None or not item.binary.resolve().is_relative_to(store.resolve()):
        raise RuntimeError(f'{name} binary escaped the verified store')

from hermes_cli.browser_runtime import chromium_executable

chrome = chromium_executable()
if not chrome or not Path(chrome).resolve().is_relative_to(store.resolve()):
    raise RuntimeError(f'Hermes does not resolve Chromium inside the image store: {chrome!r}')
linkage = subprocess.run(['ldd', chrome], check=True, capture_output=True, text=True, timeout=30).stdout
missing = sorted({line.split()[0] for line in linkage.splitlines() if 'not found' in line})
if missing:
    raise RuntimeError(f'Chromium is missing shared libraries: {missing}')
subprocess.run([chrome, '--version'], check=True, stdout=subprocess.DEVNULL, timeout=30)
print(json.dumps({'result': 'prepared', 'tools': {name: verified.entries[name].version for name in names},
                  'chromium': chrome, 'missing_libraries': [], 'venv_selection_preserved': True}, sort_keys=True))
