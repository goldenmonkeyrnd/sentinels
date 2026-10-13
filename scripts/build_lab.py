#!/usr/bin/env python3
"""Build a self-hosted, checksum-pinned lab archive from the released Python wheel."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from release_artifacts import VERSION, release_wheel

ROOT = Path(__file__).resolve().parent.parent
MAX_ASSET = 25 * 1024 * 1024

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def build(wheel: Path, output: Path, cache: Path) -> Path:
    wheel = release_wheel(wheel)
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    source_dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
    pins = json.loads((ROOT / 'lab/runtime-pins.json').read_text())
    if pins['version'] != '314.0.7':
        raise ValueError('Unexpected runtime version')
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / 'runtime'
    runtime.mkdir(exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    def fetch(item):
        name, expected = item
        path = cache / name
        data = path.read_bytes() if path.is_file() else urllib.request.urlopen(pins['base_url'] + name, timeout=60).read()
        if digest(data) != expected['sha256'] or len(data) != expected['bytes']:
            raise ValueError(f'Runtime asset checksum or size mismatch: {name}')
        path.write_bytes(data)
        (runtime / name).write_bytes(data)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, pins['assets'].items()))
    # The full lock is pinned, while only the five required dependency wheels are hosted.
    lock = json.loads((runtime / 'pyodide-lock.json').read_text())
    for package, version in {'pydantic': '2.12.5', 'pydantic-core': '2.41.5'}.items():
        if lock['packages'][package]['version'] != version:
            raise ValueError(f'Unexpected {package} version')
    for filename in ('index.html', 'lab.css'):
        shutil.copyfile(ROOT / 'lab' / filename, output / filename)
    subprocess.run(['bun', 'build', str(ROOT / 'lab/app.ts'), str(ROOT / 'lab/worker.ts'), '--outdir', str(output), '--target', 'browser', '--minify'], check=True, cwd=ROOT)
    corpus = ROOT / 'dist/browser-corpus.json'
    if not corpus.is_file():
        raise ValueError('Run uv run python scripts/export_browser_corpus.py before building the lab.')
    shutil.copyfile(corpus, output / 'qualification-corpus.json')
    wheel_bytes = wheel.read_bytes()
    corpus_data = json.loads(corpus.read_text())
    if corpus_data.get('wheel_sha256') != digest(wheel_bytes):
        raise ValueError('Parity corpus must qualify the exact bundled wheel')
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or f'Version: {VERSION}\n' not in archive.read(metadata[0]).decode():
            raise ValueError('Wheel metadata must match the lab version')
    (output / wheel.name).write_bytes(wheel_bytes)
    schema_version = json.loads((ROOT / 'examples/connection-repaired.json').read_text())['version']
    tag = f'v{VERSION}'
    release_url = f'https://github.com/goldenmonkeyrnd/sentinels/releases/tag/{tag}'
    (output / 'manifest.json').write_text(json.dumps({'version': VERSION, 'runtime': pins['version'], 'wheel': {'file': wheel.name, 'sha256': digest(wheel_bytes)}, 'release': {'tag': tag, 'url': release_url, 'source_url': f'https://github.com/goldenmonkeyrnd/sentinels/tree/{source_commit}', 'source_archive_url': f'https://github.com/goldenmonkeyrnd/sentinels/releases/download/{tag}/sentinels-source-{tag}.tar.gz'}, 'source_commit': source_commit, 'source_dirty': source_dirty, 'schema_version': schema_version, 'limits': {'files': 20, 'file_bytes': 16384, 'total_bytes': 102400, 'initialization_ms': 30000, 'analysis_ms': 5000}}, indent=2) + '\n')
    shutil.copyfile(ROOT / 'LICENSE', output / 'LICENSE')
    shutil.copyfile(ROOT / 'NOTICE', output / 'NOTICE')
    notices = [f'Sentinels is AGPL-3.0-only. Corresponding source: {release_url}', '', 'Pyodide 314.0.7: Mozilla Public License 2.0; CPython 3.14.2: Python Software Foundation License.', 'Pyodide corresponding source: https://github.com/pyodide/pyodide/tree/314.0.7', 'CPython source: https://github.com/python/cpython/tree/v3.14.2', 'Runtime provenance and hashes: runtime-pins.json. See licenses/ for exact texts and dependency notices.', 'Pydantic 2.12.5, pydantic-core 2.41.5, typing-extensions 4.15.0, annotated-types 0.7.0, typing-inspection 0.4.2 are distributed under their respective licenses below.', '']
    licenses = output / 'licenses'
    licenses.mkdir(exist_ok=True)
    license_names = set()
    for path in sorted(runtime.glob('*.whl')):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if '.dist-info/' in name and any(fragment in name.upper() for fragment in ('LICENSE', 'NOTICE', 'COPYING')) and not name.endswith('/'):
                    safe = path.name.split('-')[0] + '-' + name.replace('/', '-')
                    text = archive.read(name)
                    (licenses / safe).write_bytes(text)
                    license_names.add(safe)
                    notices.extend([f'--- {path.name}: {name} ---', text.decode('utf-8', errors='replace'), ''])
    for name in ('PYODIDE-LICENSE', 'PYTHON-LICENSE'):
        shutil.copyfile(ROOT / 'lab/licenses' / name, licenses / name)
        license_names.add(name)
    (output / 'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(notices))
    shutil.copyfile(ROOT / 'lab/runtime-pins.json', output / 'runtime-pins.json')
    # Execute only a fixed trusted counterexample. Edited project input is never executed.
    fixed = 'def validate():\n    print("validated")\n\ndef main(should_validate):\n    if should_validate:\n        validate()\n    return "published"\n'
    namespace = {}
    captured = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(captured):
        exec(compile(fixed, 'fixed_counterexample.py', 'exec'), namespace)
        returned = namespace['main'](False)
    import platform
    native_python = ROOT / '.venv/bin/python'
    if not native_python.is_file():
        raise ValueError('Run uv sync --locked before building the lab.')
    bridge = 'import json,sys; sys.path.insert(0,sys.argv[1]); from sentinels import analyze_sources; payload=json.loads(sys.stdin.read()); print(analyze_sources(payload["files"],payload["config"]).model_dump_json())'
    fixed_bundle = {'files': {'fixed_counterexample.py': fixed}, 'config': {'version': schema_version, 'contracts': [{'kind': 'connection', 'id': 'fixed-runtime-boundary', 'entry': 'fixed_counterexample:main', 'target': 'fixed_counterexample:validate'}]}}
    checked = subprocess.run([str(native_python), '-c', bridge, str(wheel)], input=json.dumps(fixed_bundle), text=True, capture_output=True, check=True)
    structural_report = json.loads(checked.stdout)
    if structural_report['status'] != 'PASS':
        raise ValueError('The fixed runtime counterexample must have a supported structural connection.')
    evidence = {'version': 1, 'fixture': 'fixed runtime bypass counterexample', 'source': fixed, 'source_sha256': digest(fixed.encode()), 'analysis': structural_report, 'checker_wheel_sha256': digest(wheel_bytes), 'input': {'should_validate': False}, 'execution': {'return': returned, 'stdout': captured.getvalue(), 'python': platform.python_version(), 'kind': 'observed fixed native fixture'}, 'boundary': 'This record is separate from analysis of edited source. Browser project source is never executed.'}
    (output / 'counterexample.json').write_text(json.dumps(evidence, indent=2) + '\n')
    expected_files = set(pins['assets'])
    expected_paths = {f'runtime/{name}' for name in expected_files}
    expected_paths.update({'index.html', 'lab.css', 'app.js', 'worker.js', 'manifest.json', wheel.name, 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.txt', 'runtime-pins.json', 'counterexample.json', 'qualification-corpus.json'})
    expected_paths.update(f'licenses/{name}' for name in license_names)
    files = {}
    for relative_path in sorted(expected_paths):
        path = output / relative_path
        if path.is_file() and path.name != 'assets-manifest.json':
            data = path.read_bytes()
            if len(data) > MAX_ASSET:
                raise ValueError(f'Asset exceeds Cloudflare 25 MiB limit: {path}')
            files[relative_path] = {'sha256': digest(data), 'bytes': len(data)}
    (output / 'assets-manifest.json').write_text(json.dumps({'version': VERSION, 'files': files}, indent=2) + '\n')
    archive_path = output.parent / f'sentinels-lab-v{VERSION}.tar.gz'
    # USTAR, deterministic metadata, no links, no extended archive records.
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for relative_path in sorted([*files, 'assets-manifest.json']):
            path = output / relative_path
            info = tarfile.TarInfo(relative_path)
            info.size = path.stat().st_size
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(path.read_bytes()))
    with archive_path.open('wb') as target:
        with gzip.GzipFile(fileobj=target, mode='wb', filename='', mtime=0) as compressed:
            compressed.write(raw.getvalue())
    print(json.dumps({'archive': str(archive_path), 'sha256': digest(archive_path.read_bytes()), 'files': len(files), 'bytes': archive_path.stat().st_size}))
    return archive_path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/lab')
    parser.add_argument('--cache', type=Path, default=Path('/tmp/sentinels-runtime-cache'))
    args = parser.parse_args()
    build(release_wheel(args.wheel), args.output.resolve(), args.cache.resolve())

if __name__ == '__main__':
    main()
