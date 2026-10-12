/** Real Pyodide qualification; uses the released wheel, not a JavaScript substitute. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, stat, mkdir, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { resolve, extname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
const root = resolve(fileURLToPath(new URL('..', import.meta.url)));
const directory = resolve(process.env.SENTINELS_LAB_DIR ?? root + '/dist/lab');
const results = [], requests = [], consoleErrors = [];
const csp = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'";
const server = createServer(async (req, res) => {
    try { const path = resolve(directory, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname)); if (relative(directory, path).startsWith('..')) { res.writeHead(400).end(); return; } const target = (await stat(path)).isDirectory() ? resolve(path, 'index.html') : path; const data = await readFile(target); const types = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.json': 'application/json', '.wasm': 'application/wasm', '.css': 'text/css' }; res.writeHead(200, { 'Content-Type': types[extname(target)] ?? 'application/octet-stream', 'Content-Security-Policy': csp, 'Cache-Control': 'no-store' }); res.end(data); } catch { res.writeHead(404).end('Not found'); }
});
await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
const base = `http://127.0.0.1:${server.address().port}/`;
let browser;
function semantic(report) { return { status: report.status, scope: report.scope, contracts: report.contracts, edges: report.edges, assumptions: report.assumptions, errors: report.errors, source_hash: report.source_hash, config_hash: report.config_hash }; }
function native(bundlePath) { const command = spawnSync(resolve(root, '.venv/bin/python'), ['-m', 'sentinels', 'check', '--bundle', bundlePath, '--json'], { cwd: root, encoding: 'utf8' }); if (![0, 1, 2, 3].includes(command.status)) throw new Error(command.stderr || command.stdout); return JSON.parse(command.stdout); }
try {
    browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
    const context = await browser.newContext(); const page = await context.newPage();
    page.on('request', request => requests.push({ url: request.url(), method: request.method(), body: request.postData() }));
    page.on('pageerror', error => consoleErrors.push(String(error)));
    await page.goto(base); await page.waitForLoadState('networkidle');
    assert.equal(requests.filter(request => /runtime\/|\.whl|\.wasm|pyodide/.test(request.url)).length, 0, 'No runtime downloads before activation'); results.push('zero runtime downloads before activation');
    assert.equal(await page.locator('#source-editor').getAttribute('aria-describedby'), 'source-help');
    await page.getByRole('button', { name: 'Try the lab', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('#run').disabled, null, { timeout: 32_000 });
    assert.match(await page.locator('#runtime-status').innerText(), /Ready/); results.push('real Pyodide314.0.7 + Pydantic2.12.5 + released wheel initialized');
    async function runReport() { await page.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('#run').disabled, null, { timeout: 7000 }); const raw = await page.locator('#report-json').textContent(); assert.ok(raw, await page.locator('#run-status').innerText()); return JSON.parse(raw); }
    for (const story of ['connection', 'handoff', 'shape']) {
        await page.getByRole('button', { name: ({ connection: '1. The disconnected validator', handoff: '2. The abandoned calculation', shape: '3. The drifting snapshot' })[story], exact: true }).click();
        for (const state of ['broken', 'repaired']) {
            if (state === 'repaired') await page.getByRole('button', { name: 'Load repair', exact: true }).click();
            const report = await runReport();
            assert.equal(report.status, state === 'broken' ? 'FAIL' : 'PASS', `${story} ${state}: ${JSON.stringify(report.contracts)}`);
            assert.deepEqual(semantic(report), semantic(native(resolve(root, `examples/${story}-${state}.json`))), `${story} ${state} browser/native semantic parity`);
            assert.equal(await page.locator('#download-report').isDisabled(), false);
            if (story === 'shape' && state === 'broken') {
                await page.locator('#report-content .finding-link').first().click();
                assert.equal(await page.evaluate(() => document.activeElement.id), 'source-editor');
                const linked = report.contracts.flatMap(contract => contract.findings).find(finding => finding.location)?.location; const sourceLines = (await page.locator('#source-editor').inputValue()).split('\n'); assert.equal(await page.evaluate(() => { const editor = document.querySelector('#source-editor'); return editor.value.slice(editor.selectionStart, editor.selectionEnd); }), sourceLines[linked.line - 1]);
                results.push('finding links focus and select the affected source line');
            }
            results.push(`${story} ${state}: real browser/native findings and witness parity`);
        }
    }
    const projectDownload = page.waitForEvent('download'); await page.getByRole('button', { name: 'Download project', exact: true }).click();
    const downloadedProject = JSON.parse(await readFile(await (await projectDownload).path(), 'utf8')); assert.equal(downloadedProject.version, 1); assert.equal(downloadedProject.files['pipeline.py'], await page.locator('#source-editor').inputValue());
    const reportDownload = page.waitForEvent('download'); await page.getByRole('button', { name: 'Download report', exact: true }).click(); const downloadedReport = JSON.parse(await readFile(await (await reportDownload).path(), 'utf8')); assert.equal(downloadedReport.status, 'PASS'); results.push('project and report downloads reproduce current input and result');
    const corpusResults = await page.evaluate(async () => {
        const corpus = await (await fetch('./qualification-corpus.json')).json();
        const manifest = await (await fetch('./manifest.json')).json();
        if (corpus.wheel_sha256 !== manifest.wheel.sha256) throw new Error('Corpus wheel hash must match the browser wheel');
        const worker = new Worker('./worker.js', { type: 'module' }); let serial = 0; const pending = new Map();
        worker.onmessage = event => { const item = pending.get(event.data.id); if (!item) return; clearTimeout(item.timer); pending.delete(event.data.id); event.data.kind === 'error' ? item.reject(new Error(event.data.error)) : item.resolve(event.data); };
        const request = (kind, bundle) => new Promise((resolve, reject) => { const id = ++serial; const timer = setTimeout(() => { pending.delete(id); reject(new Error('Corpus worker timeout')); }, kind === 'initialize' ? 30000 : 5000); pending.set(id, { resolve, reject, timer }); worker.postMessage({ id, kind, bundle }); });
        const captured = [];
        try { await request('initialize'); for (const fixture of corpus.cases) { const result = await request('analyze', fixture.bundle); captured.push({ id: fixture.id, expected: fixture.report, actual: result.report }); } } finally { worker.terminate(); }
        return captured;
    });
    assert.ok(corpusResults.length >= 50, 'Broad native fixture corpus is available');
    for (const fixture of corpusResults) assert.deepEqual(semantic(fixture.actual), semantic(fixture.expected), `Native/browser adversarial fixture ${fixture.id}`);
    results.push(`${corpusResults.length} additional bounded native fixtures: real Pyodide semantic parity`);
    await page.locator('#source-editor').fill((await page.locator('#source-editor').inputValue()) + '\nraise RuntimeError("SUBMITTED_SOURCE_MUST_NOT_EXECUTE")\n');
    assert.match(await page.locator('#report-content .verdict').first().innerText(), /STALE/); assert.equal(await page.locator('#download-report').isDisabled(), true); results.push('edits immediately invalidate results');
    const sideEffectReport = await runReport(); assert.equal(sideEffectReport.status, 'PASS'); results.push('submitted import-time side effect remains unexecuted');
    await context.setOffline(true); await page.locator('#source-editor').fill((await page.locator('#source-editor').inputValue()).replace('"residents": 1200', '"residents": 1201')); assert.equal((await runReport()).status, 'PASS'); results.push('offline analysis after initialization'); await context.setOffline(false);
    await page.locator('#source-editor').fill('x'.repeat(16 * 1024 + 1)); await page.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); assert.match(await page.locator('#run-status').innerText(), /exceeds 16 KiB/); results.push('browser input byte limits');
    await page.getByRole('button', { name: 'Load repair', exact: true }).click();
    const strictConfig = JSON.parse(await page.locator('#contract-editor').inputValue()); strictConfig.unexpected = true; await page.locator('#contract-editor').fill(JSON.stringify(strictConfig)); await page.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('#run').disabled); assert.match(await page.locator('#run-status').innerText(), /ERROR/); assert.equal(await page.locator('#download-report').isDisabled(), true); results.push('strict browser configuration rejects unknown keys'); await page.getByRole('button', { name: 'Load repair', exact: true }).click();
    await page.locator('#source-editor').focus(); await page.keyboard.press('Tab'); assert.notEqual(await page.evaluate(() => document.activeElement.id), 'source-editor'); results.push('editors preserve keyboard Tab navigation');
    await page.setViewportSize({ width: 375, height: 812 }); assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); results.push('375px mobile layout has no horizontal page overflow');
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0); assert.ok(requests.every(request => request.url.startsWith(base) && request.method === 'GET' && request.body === null), 'Only same-origin asset requests, no source uploads'); assert.deepEqual(consoleErrors, []); results.push('source privacy: no uploads, storage, telemetry, or external requests');
    await page.screenshot({ path: '/tmp/sentinels-lab-mobile.png', fullPage: true });
    // A controlled stalled worker exercises the UI watchdog independently of analyzer speed.
    const recovery = await browser.newContext(); await recovery.route('**/worker.js', route => route.fulfill({ contentType: 'text/javascript', body: `self.onmessage=e=>{if(e.data.kind==='initialize')self.postMessage({id:e.data.id,kind:'ready'});};` }));
    const stalled = await recovery.newPage(); await stalled.goto(base); await stalled.getByRole('button', { name: 'Try the lab', exact: true }).click(); await stalled.waitForFunction(() => !document.querySelector('#run').disabled);
    const preserved = await stalled.locator('#source-editor').inputValue(); await stalled.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await stalled.waitForFunction(() => document.querySelector('#run-status').textContent.includes('five seconds'), null, { timeout: 7000 }); assert.equal(await stalled.locator('#source-editor').inputValue(), preserved); results.push('five-second watchdog terminates worker and preserves edits');
    await recovery.unroute('**/worker.js'); await stalled.getByRole('button', { name: 'Initialize checker again', exact: true }).click(); await stalled.waitForFunction(() => !document.querySelector('#run').disabled, null, { timeout: 32000 }); assert.equal(await stalled.locator('#source-editor').inputValue(), preserved); results.push('real worker reinitializes after watchdog failure');
    // Cancellation uses a second controlled worker, preserving the same project.
    await stalled.close(); await recovery.close();
    const cancellation = await browser.newContext(); await cancellation.route('**/worker.js', route => route.fulfill({ contentType: 'text/javascript', body: `self.onmessage=e=>{if(e.data.kind==='initialize')self.postMessage({id:e.data.id,kind:'ready'});};` })); const canceled = await cancellation.newPage(); await canceled.goto(base); await canceled.getByRole('button', { name: 'Try the lab', exact: true }).click(); await canceled.waitForFunction(() => !document.querySelector('#run').disabled); const beforeCancel = await canceled.locator('#source-editor').inputValue(); await canceled.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await canceled.getByRole('button', { name: 'Cancel analysis', exact: true }).click(); assert.match(await canceled.locator('#run-status').innerText(), /canceled/); assert.equal(await canceled.locator('#source-editor').inputValue(), beforeCancel); results.push('explicit cancellation preserves edits'); await cancellation.close();
    // Initialization timeout is also enforced, rather than waiting indefinitely.
    const timeoutContext = await browser.newContext(); await timeoutContext.route('**/worker.js', route => route.fulfill({ contentType: 'text/javascript', body: 'self.onmessage=()=>{};' })); const timed = await timeoutContext.newPage(); await timed.goto(base); const beforeTimeout = await timed.locator('#source-editor').inputValue(); await timed.getByRole('button', { name: 'Try the lab', exact: true }).click(); await timed.waitForFunction(() => document.querySelector('#run-status').textContent.includes('30 seconds'), null, { timeout: 32000 }); assert.equal(await timed.locator('#source-editor').inputValue(), beforeTimeout); results.push('30-second initialization watchdog preserves edits'); await timeoutContext.close();
    if (process.env.SENTINELS_PORTFOLIO_URL) {
        const siteContext = await browser.newContext({ extraHTTPHeaders: { DNT: '1', 'Sec-GPC': '1' } }); await siteContext.addInitScript(() => { Object.defineProperty(navigator, 'globalPrivacyControl', { value: true }); Object.defineProperty(navigator, 'doNotTrack', { value: '1' }); }); const site = await siteContext.newPage(), siteRequests = []; site.on('request', request => siteRequests.push(request.url()));
        const mainResponse = await site.goto(process.env.SENTINELS_PORTFOLIO_URL); await site.waitForLoadState('networkidle'); assert.equal(siteRequests.filter(url => url.includes('/labs/sentinels/')).length, 0, 'Portfolio does not load lab before activation'); assert.ok(!(await mainResponse.allHeaders())['content-security-policy'].includes('wasm-unsafe-eval'), 'Main site policy remains narrow');
        await site.locator('[data-sentinels-activate]').click(); const frame = site.frameLocator('[data-sentinels-frame] iframe'); await frame.getByRole('button', { name: 'Try the lab', exact: true }).waitFor(); assert.equal(siteRequests.filter(url => /runtime\/|\.whl|\.wasm/.test(url)).length, 0, 'Embedded runtime waits for its activation'); await frame.getByRole('button', { name: 'Try the lab', exact: true }).click(); await frame.locator('#run:not([disabled])').waitFor({ timeout: 32000 }); await frame.getByRole('button', { name: 'Load repair', exact: true }).click(); await frame.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await frame.locator('#run:not([disabled])').waitFor({ timeout: 7000 }); assert.equal(JSON.parse(await frame.locator('#report-json').textContent()).status, 'PASS'); const labURL = new URL('/labs/sentinels/v0.1.0/', process.env.SENTINELS_PORTFOLIO_URL); const labResponse = await siteContext.request.get(labURL.href); const labCSP = labResponse.headers()['content-security-policy']; assert.ok(labCSP.includes("'wasm-unsafe-eval'") && labCSP.includes("frame-ancestors 'self'"), 'Lab policy permits Wasm and same-origin framing'); results.push('portfolio activation, DNT/GPC independence, scoped headers, and embedded visitor-generated PASS'); await siteContext.close();
    }
    await mkdir(resolve(root, 'dist'), { recursive: true }); await writeFile(resolve(root, 'dist/browser-qualification.json'), JSON.stringify({ tests: results, parser_version: JSON.parse(await page.locator('#report-json').textContent()).parser_version, checker_version: JSON.parse(await page.locator('#report-json').textContent()).checker_version, pyodide: '314.0.7', browser: await browser.version(), wheel_manifest: JSON.parse(await readFile(resolve(directory, 'manifest.json'), 'utf8')), fixture_cases: 6 + corpusResults.length }, null, 2) + '\n');
    console.log(JSON.stringify({ passed: results.length, results }, null, 2));
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
