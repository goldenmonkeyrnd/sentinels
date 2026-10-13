"""Capture bounded native analysis fixtures for the exact browser semantic parity test."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import argparse
from release_artifacts import release_wheel

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--wheel', type=Path)
args = parser.parse_args()
wheel = release_wheel(args.wheel)
if not wheel.is_file():
    raise SystemExit('Build the release wheel before exporting its browser parity corpus')
sys.path.insert(0, str(wheel))
import sentinels
import sentinels.analyzer as analyzer
from sentinels.models import Bundle
if str(wheel) not in sentinels.__file__:
    raise SystemExit('Parity fixtures must use the exact release wheel')
cases: dict[str, dict[str, object]] = {}
original = analyzer.analyze_sources

def capture(files, config):
    report = original(files, config)
    # Browser inputs use the strict bundle boundary. Invalid configurations are tested
    # separately, and native filesystem-only operations cannot be browser projects.
    raw = config.model_dump(mode='json') if hasattr(config, 'model_dump') else config
    payload = {'version': Bundle.model_fields['version'].default, 'files': dict(files), 'config': raw}
    try:
        bundle = Bundle.model_validate_json(json.dumps(payload))
    except Exception:
        return report
    encoded = {path: content.encode('utf-8') for path, content in bundle.files.items()}
    if not encoded or len(encoded) > 20 or any(len(content) > 16384 for content in encoded.values()) or sum(map(len, encoded.values())) > 102400:
        return report
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    identity = hashlib.sha256(canonical.encode()).hexdigest()
    cases[identity] = {'id': identity, 'bundle': payload, 'report': report.model_dump(mode='json')}
    return report

with patch.object(analyzer, 'analyze_sources', capture):
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
if not result.wasSuccessful() or result.skipped:
    raise SystemExit('Native tests must pass before exporting browser parity fixtures')
# Tests deliberately mutate checker dispatch as well as application inputs.
# Recompute with the restored released checker, so no injected checker defect
# becomes an alleged native/browser semantic expectation.
for case in cases.values():
    payload = case['bundle']
    case['report'] = original(payload['files'], payload['config']).model_dump(mode='json')
output = ROOT / 'dist/browser-corpus.json'
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps({'version': 1, 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'native_tests': result.testsRun, 'cases': [cases[key] for key in sorted(cases)]}, indent=2) + '\n')
print(json.dumps({'path': str(output), 'cases': len(cases), 'native_tests': result.testsRun}))
