"""Fetch upstream runtime assets for initial pinning; never part of a normal build."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import urllib.request

BASE = 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/'
CACHE = Path('/tmp/sentinels-runtime-cache')
CACHE.mkdir(exist_ok=True)
NAMES = ('pyodide.mjs', 'pyodide.asm.mjs', 'pyodide.asm.wasm', 'python_stdlib.zip', 'pyodide-lock.json')
def get(name):
    data = urllib.request.urlopen(BASE + name, timeout=60).read()
    (CACHE / name).write_bytes(data)
    return name, {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
with ThreadPoolExecutor(max_workers=4) as pool:
    pins = dict(pool.map(get, NAMES))
lock = json.loads((CACHE / 'pyodide-lock.json').read_text())
closure = ['pydantic', 'pydantic-core', 'typing-extensions', 'annotated-types', 'typing-inspection']
for name in closure:
    info = lock['packages'][name]
    filename, fetched = get(info['file_name'])
    if fetched['sha256'] != info['sha256']:
        raise SystemExit(f'Upstream wheel hash mismatch: {name}')
    pins[filename] = {**fetched, 'package': name, 'version': info['version']}
print(json.dumps({'version': '314.0.7', 'base_url': BASE, 'assets': pins}, indent=2))
