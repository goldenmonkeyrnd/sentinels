"""Qualify tests and real CLI examples using the exact built wheel."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import unittest

from release_artifacts import ROOT, VERSION, release_wheel

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--wheel', type=Path)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
wheel = release_wheel(args.wheel)
sys.path.insert(0, str(wheel))
import sentinels

if str(wheel) not in sentinels.__file__ or sentinels.__version__ != VERSION:
    raise SystemExit('Qualification must import the exact release wheel')
result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(str(ROOT / 'tests')))
if not result.wasSuccessful() or result.skipped:
    raise SystemExit('Qualification requires all native and adapter tests to pass without skips')

bridge = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from sentinels.cli import main; raise SystemExit(main())'
examples = []
for path in sorted((ROOT / 'examples').glob('*.json')):
    expected = 1 if path.stem.endswith('-broken') else 0
    run = subprocess.run([sys.executable, '-c', bridge, str(wheel), 'check', '--bundle', str(path), '--json'], capture_output=True, text=True)
    report = json.loads(run.stdout)
    if run.returncode != expected or report['checker_version'] != VERSION:
        raise SystemExit(f'Released CLI failed {path.name}: {run.stdout} {run.stderr}')
    examples.append({'bundle': path.name, 'status': report['status'], 'exit_code': run.returncode})
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps({'version': 1, 'wheel': {'file': wheel.name, 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}, 'python': platform.python_version(), 'tests': result.testsRun, 'failures': 0, 'errors': 0, 'skipped': 0, 'cli_examples': examples}, indent=2) + '\n')
