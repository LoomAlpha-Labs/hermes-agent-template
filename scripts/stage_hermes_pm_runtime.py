#!/usr/bin/env python3
"""Stage the pinned core's independent PM runtime in the template image.

The template's Python lives outside the Hermes tree, unlike the upstream
Docker payload. Use the upstream staging primitive and the absolute-path
resident marker supported by Docker/Nix runtime discovery.
"""
import json
from pathlib import Path
import subprocess
import sys

from pm.runtime_stage import stage_runtime

root = Path('/opt/hermes-agent')
runtime = root / 'pm-runtime'
python = Path('/usr/local/bin/python').resolve()
uv = Path('/usr/local/bin/uv')
stage_runtime(uv, python, runtime, project=root / 'pm')
sites = list(runtime.glob('lib/python*/site-packages'))
if len(sites) != 1:
    raise RuntimeError('PM staging must produce exactly one site-packages directory')
site = sites[0].resolve()
marker = {'python': str(python), 'sitePackages': str(site)}
(runtime / 'pm-runtime.json').write_text(json.dumps(marker) + '\n', encoding='utf-8')
stamp_path = root / 'install-stamp.json'
stamp = json.loads(stamp_path.read_text(encoding='utf-8'))
if stamp.get('distribution') != 'docker':
    raise RuntimeError('expected Docker install stamp')
stamp['pmRuntime'] = str(runtime)
stamp['runtimeDir'] = str(root / 'tools')
stamp_path.write_text(json.dumps(stamp) + '\n', encoding='utf-8')

# Verify the same isolated launch mechanism used for package-manager workers.
from pm.runtime import runtime_command
command = runtime_command(root / 'pm/launch.py', ['--help'])
subprocess.run(command, check=True, stdout=subprocess.DEVNULL, timeout=30)
subprocess.run([str(python), '-I', '-S', '-c',
                'import sys; sys.path.insert(0,sys.argv[1]); '
                'import packaging, tomli_w, truststore; from ruamel.yaml import YAML',
                str(site)], check=True, timeout=30)
print('Independent packaged PM runtime passed')
