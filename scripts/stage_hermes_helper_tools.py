#!/usr/bin/env python3
"""Prepare upstream-locked helper tools into the image-owned PM store."""
import json
from pathlib import Path

import pm
from pm.build_operations import verified_tools
from pm.lock import Facts
from pm.store import current_target

store = Path('/opt/hermes-agent/tools')
names = ['node', 'npm', 'ffmpeg', 'ripgrep']
before = Facts(store / 'facts.json', strict=True).get('venv')
if not before:
    raise RuntimeError('Missing packaged application venv selection')
pm.prepare_tools(names, out=store, target=current_target())
after = Facts(store / 'facts.json', strict=True).get('venv')
if before != after:
    raise RuntimeError('Preparing tools changed the packaged venv selection')
verified = verified_tools(names, source_store=store, target=current_target())
for name in names:
    item = verified.entries[name]
    if item.binary is None or not item.binary.resolve().is_relative_to(store.resolve()):
        raise RuntimeError(f'{name} binary escaped the verified store')
print(json.dumps({'result': 'prepared', 'tools': {name: verified.entries[name].version for name in names},
                  'venv_selection_preserved': True}, sort_keys=True))
