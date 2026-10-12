// Only trusted checker code runs in this worker. Submitted Python remains JSON data.
interface Pyodide { loadPackage(names: string[], options?: object): Promise<void>; unpackArchive(data: Uint8Array, format: string): void; runPythonAsync(source: string): Promise<unknown>; globals: { set(key: string, value: unknown): void; delete(key: string): void } }
interface RuntimeManifest { runtime: string; wheel: { file: string; sha256: string }; version: string }
let pyodide: Pyodide | undefined;
let initializing: Promise<void> | undefined;
async function sha256(bytes: ArrayBuffer): Promise<string> { return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join(''); }
async function initialize() {
    const manifest = await (await fetch('./manifest.json')).json() as RuntimeManifest;
    const runtime = new URL('./runtime/', self.location.href).href;
    const module = await import(/* @vite-ignore */ new URL('pyodide.mjs', runtime).href);
    pyodide = await module.loadPyodide({ indexURL: runtime, lockFileURL: new URL('pyodide-lock.json', runtime).href, packageBaseUrl: runtime }) as Pyodide;
    await pyodide.loadPackage(['pydantic'], { checkIntegrity: true });
    const response = await fetch(new URL(manifest.wheel.file, self.location.href));
    if (!response.ok) throw new Error(`Checker wheel failed to load (${response.status})`);
    const bytes = await response.arrayBuffer();
    if (await sha256(bytes) !== manifest.wheel.sha256) throw new Error('Checker wheel checksum mismatch');
    pyodide.unpackArchive(new Uint8Array(bytes), 'zip');
    // Analyzer source discovery is not used here; all inputs come through the strict bundle boundary.
    await pyodide.runPythonAsync('import sentinels\nfrom sentinels.models import ProjectBundle\nfrom sentinels.cli import _error_report, _input_error_message\nfrom pydantic import ValidationError\nimport json');
}
self.onmessage = async (event: MessageEvent) => {
    const { id, kind, bundle } = event.data;
    try {
        if (kind === 'initialize') {
            initializing ??= initialize();
            await initializing;
            self.postMessage({ id, kind: 'ready' });
        } else if (kind === 'analyze') {
            if (!pyodide) throw new Error('Checker has not initialized');
            pyodide.globals.set('_sentinels_input_json', JSON.stringify(bundle));
            try {
                const result = await pyodide.runPythonAsync(`try:
    _sentinels_bundle = ProjectBundle.model_validate_json(_sentinels_input_json)
except (ValidationError, ValueError, TypeError) as _sentinels_error:
    _sentinels_result = _error_report(_input_error_message(_sentinels_error))
else:
    _sentinels_result = sentinels.analyze_sources(_sentinels_bundle.files, _sentinels_bundle.config)
_sentinels_result.model_dump_json()`);
                self.postMessage({ id, kind: 'report', report: JSON.parse(String(result)) });
            } finally { pyodide.globals.delete('_sentinels_input_json'); }
        } else { throw new Error('Unknown worker request'); }
    } catch (error) { self.postMessage({ id, kind: 'error', error: error instanceof Error ? error.message : String(error) }); }
};
