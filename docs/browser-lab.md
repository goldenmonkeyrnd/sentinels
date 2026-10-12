# Running and qualifying the browser lab

The lab runs the exact release wheel in a module Web Worker. It uses Python AST and symbol-table analysis, not a second JavaScript checker. Submitted source is JSON data passed to `analyze_sources`; it is never supplied to `runPythonAsync`, imported as an application, or executed. The trusted bridge validates a version 1 bundle with the same Pydantic model as the CLI.

Use Python 3.12–3.14, uv, and Bun 1.3.4:

```sh
uv sync --locked
bun install --frozen-lockfile
bun run build
bunx playwright install --with-deps chromium
bun run test:browser
python3 -m http.server --directory dist/lab 8080
```

Open `http://localhost:8080/`. Select **Try the lab**, wait for initialization, edit either source or contracts, and run fresh analysis. Download the project to reproduce the browser input locally:

```sh
uv run sentinels check --bundle sentinels-project.json --json
```

`bun run build` builds the Python wheel, runs the native tests to export their bounded analysis fixtures, and builds the browser archive. `bun run build:lab` uses the already-built wheel and corpus; regenerate the corpus with `uv run python scripts/export_browser_corpus.py` when native fixtures change. All runtime downloads are checked against committed `lab/runtime-pins.json`, including the upstream lock and the five Pydantic dependency wheels. The default cache is `/tmp/sentinels-runtime-cache`; `python3 scripts/build_lab.py --wheel … --cache … --output …` can override it. No CDN or PyPI request occurs in the visitor's browser. The complete upstream lock is hosted locally, but only the required dependency closure is distributed.

The built directory `dist/lab/` contains `index.html`, app and worker scripts, the Python wheel, minimal Pyodide runtime, dependency wheels, license texts, a fixed counterexample record, and checksums. The USTAR archive `dist/sentinels-lab-v0.1.0.tar.gz` has regular files only and `index.html` at its root. `assets-manifest.json` maps every other file to its SHA-256 and byte size. Every file is below Cloudflare's 25 MiB asset limit. Archive metadata is deterministic; the fixed execution record reports the build interpreter version.

`bun run test:browser` starts its own temporary local HTTP server with the lab's CSP and launches real Chromium. It checks lazy runtime loading, all six broken/repaired examples and the full bounded native fixture corpus against native semantic reports, source privacy, offline analysis after initialization, stale results, limits, keyboard navigation, mobile layout, and timeout/cancellation recovery. Only the controlled stalled-worker tests substitute the worker to exercise the watchdog; all analyzer findings use real Pyodide and the wheel. It writes `dist/browser-qualification.json`, containing the exact wheel hash and browser version. Set `SENTINELS_PORTFOLIO_URL` to a local Workers preview or deployed article URL to also verify embedding, GPC/DNT independence, scoped headers, and a fresh visitor result. `CHROMIUM_EXECUTABLE` can select an already-installed Chromium binary; otherwise install the version pinned by Playwright.

## Deployment policy

Serve lab paths with:

```text
Content-Security-Policy: default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'
```

Apply this only to the lab routes. Same-origin framing supports the portfolio embed. The lab loader does not depend on analytics, GPC, or DNT. The runtime is fetched only after activation. There is no source upload, content telemetry, local storage, session storage, or automatic persistence. Visitors can download their work manually.

Twenty files, 16 KiB per file, and 100 KiB total source are enforced before starting analysis. One job runs at a time. Initialization has a 30-second watchdog; analysis has a five-second watchdog. Cancellation or timeout terminates the worker and preserves the editors. Use **Initialize checker again** to recover. Offline analysis works after initialization; offline page reload is not promised.

## Guided projects and runtime boundary

`lab/stories.ts` owns the six guided fixtures. `bun run examples` regenerates the equivalent CLI project bundles under `examples/`. The connection story uses a Typer application and typed class methods. The handoff story uses Prefect submission and demonstrates why ordering-only dependencies do not deliver data. The shape story contrasts literal publication fields with a declared Pydantic model.

The fixed runtime bypass example is executed only by the trusted build script. Its JSON record includes source, hash, fixed input, returned value, standard output, Python version, and a separate structural PASS with witness produced by the exact checker wheel. It is labeled separately from editable source analysis. A supported structural path does not establish all-branch execution, ordering, successful task execution, or correct output.

## Troubleshooting

- Initialization fails: verify local asset URLs, wheel checksum, CSP, MIME types, and the copied dependency closure; then initialize again.
- A result is stale: source or configuration changed after its analysis. Run again before downloading a report.
- Strict configuration error: use the exported schemas under `docs/schemas/`; unknown keys and coercible values are rejected.
- Browser and native disagree: retain the project bundle and both reports. Parser provenance may differ; contract verdicts, findings, witnesses, edges, and input hashes should match for supported input.
- Production still shows an old build: verify the exact release archive checksum, URL, and `assets-manifest.json` before diagnosing cache behavior.
