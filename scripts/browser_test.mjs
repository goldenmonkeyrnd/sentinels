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
const releaseManifest = JSON.parse(await readFile(resolve(directory, 'manifest.json'), 'utf8'));
const results = [], requests = [], consoleErrors = [];
const csp = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'";
const server = createServer(async (req, res) => {
    try { const path = resolve(directory, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname)); if (relative(directory, path).startsWith('..')) { res.writeHead(400).end(); return; } const target = (await stat(path)).isDirectory() ? resolve(path, 'index.html') : path; const data = await readFile(target); const types = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.json': 'application/json', '.wasm': 'application/wasm', '.css': 'text/css' }; res.writeHead(200, { 'Content-Type': types[extname(target)] ?? 'application/octet-stream', 'Content-Security-Policy': csp, 'Cache-Control': 'no-store' }); res.end(data); } catch { res.writeHead(404).end('Not found'); }
});
await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
const base = `http://127.0.0.1:${server.address().port}/`;
let browser;
function semantic(report) { return { version: report.version, status: report.status, scope: report.scope, contracts: report.contracts, edges: report.edges, assumptions: report.assumptions, errors: report.errors, source_hash: report.source_hash, config_hash: report.config_hash }; }
function native(bundlePath) { const wheel = resolve(directory, releaseManifest.wheel.file); const bridge = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from sentinels.cli import main; raise SystemExit(main())'; const command = spawnSync(resolve(root, '.venv/bin/python'), ['-c', bridge, wheel, 'check', '--bundle', bundlePath, '--json'], { cwd: root, encoding: 'utf8' }); if (![0, 1, 2, 3].includes(command.status)) throw new Error(command.stderr || command.stdout); return JSON.parse(command.stdout); }
async function assertProvenance(workspace, expected = releaseManifest) {
    await workspace.locator('#activate:not([disabled])').waitFor();
    assert.deepEqual(await workspace.locator('[data-release-version]').allTextContents(), [expected.release.tag, expected.release.tag]);
    for (const [id, field] of [['release-link', 'url'], ['source-link', 'source_url'], ['source-archive-link', 'source_archive_url']]) assert.equal(await workspace.locator(`#${id}`).getAttribute('href'), expected.release[field], `${id} uses bundled release metadata`);
    if (expected.source_commit) assert.match(await workspace.locator('#release-provenance').innerText(), new RegExp(expected.source_commit));
}
async function runWorkspace(workspace) {
    await workspace.getByRole('button', { name: 'Try the lab', exact: true }).click();
    await workspace.locator('#run:not([disabled])').waitFor({ timeout: 32000 });
    await workspace.getByRole('button', { name: 'Load repair', exact: true }).click();
    await workspace.getByRole('button', { name: 'Run fresh analysis', exact: true }).click();
    await workspace.locator('#run:not([disabled])').waitFor({ timeout: 7000 });
    assert.equal(JSON.parse(await workspace.locator('#report-json').textContent()).status, 'PASS');
}
async function assertReleasedWorkspace(context, iframe, documentURL) {
    const source = await iframe.getAttribute('src');
    assert.ok(source, 'Workspace iframe has a release URL');
    const url = new URL(source, documentURL);
    assert.match(url.pathname, /^\/labs\/sentinels\/_bundles\/[a-f0-9]{64}\/$/, 'Workspace is isolated at the immutable archive hash');
    const response = await context.request.get(url.href);
    assert.equal(response.status(), 200);
    const policy = response.headers()['content-security-policy'] ?? '';
    assert.ok(policy.includes("'wasm-unsafe-eval'") && policy.includes("frame-ancestors 'self'"), 'Workspace policy permits Wasm and same-origin framing');
    const manifestResponse = await context.request.get(new URL('manifest.json', url).href);
    assert.equal(manifestResponse.status(), 200);
    const manifest = await manifestResponse.json();
    assert.equal(manifest.wheel.sha256, releaseManifest.wheel.sha256, 'Website pins the qualified wheel');
    return manifest;
}
try {
    browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
    const context = await browser.newContext(); const page = await context.newPage();
    page.on('request', request => requests.push({ url: request.url(), method: request.method(), body: request.postData() }));
    page.on('pageerror', error => consoleErrors.push(String(error)));
    await page.goto(base); await page.waitForLoadState('networkidle');
    await assertProvenance(page); results.push('visible version, source identity, and exact download links come from the bundled manifest');
    assert.equal(requests.filter(request => /runtime\/|\.whl|\.wasm|pyodide/.test(request.url)).length, 0, 'No runtime downloads before activation'); results.push('zero runtime downloads before activation');
    assert.equal(await page.locator('#source-editor').getAttribute('aria-describedby'), 'source-help');
    await page.getByRole('button', { name: 'Try the lab', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('#run').disabled, null, { timeout: 32_000 });
    assert.match(await page.locator('#runtime-status').innerText(), /Ready/); results.push('real Pyodide314.0.7 + Pydantic2.12.5 + released wheel initialized');
    async function runReport() { await page.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('#run').disabled, null, { timeout: 7000 }); const raw = await page.locator('#report-json').textContent(); assert.ok(raw, await page.locator('#run-status').innerText()); return JSON.parse(raw); }
    const storyIds = ['connection', 'handoff', 'shape', 'coverage'];
    for (const story of storyIds) {
        await page.getByRole('button', { name: ({ connection: '1. The disconnected validator', handoff: '2. The abandoned calculation', shape: '3. The drifting snapshot', coverage: '4. The growing project' })[story], exact: true }).click();
        for (const state of ['broken', 'repaired']) {
            if (state === 'repaired') await page.getByRole('button', { name: 'Load repair', exact: true }).click();
            const report = await runReport();
            assert.equal(report.status, state === 'broken' ? 'FAIL' : 'PASS', `${story} ${state}: ${JSON.stringify(report.contracts)}`);
            assert.deepEqual(semantic(report), semantic(native(resolve(root, `examples/${story}-${state}.json`))), `${story} ${state} browser/native semantic parity`);
            assert.equal(report.version, 2, 'Reports use the strict version 2 boundary');
            assert.equal(await page.locator('#download-report').isDisabled(), false);
            if (story === 'coverage') {
                const inventory = report.contracts.find(contract => contract.kind === 'coverage').coverage;
                assert.deepEqual(inventory.roots, ['.']); assert.deepEqual(inventory.inspected_files, ['pipeline.py']);
                const helper = inventory.surfaces.find(surface => surface.id === 'pipeline:_county_label');
                assert.equal(helper.classification, state === 'broken' ? 'unclassified' : 'internal');
                assert.ok(report.contracts.filter(contract => contract.kind !== 'coverage').every(contract => contract.status === 'PASS'), 'Coverage growth fails independently of passing existing contracts');
                const item = page.locator('.coverage-inventory li').filter({ has: page.locator('strong', { hasText: 'pipeline:_county_label' }) });
                assert.match(await item.innerText(), state === 'broken' ? /unclassified/ : /Internal reason:/);
            }
            if (story === 'shape' && state === 'broken') {
                await page.locator('#report-content .finding-link').first().click();
                assert.equal(await page.evaluate(() => document.activeElement.id), 'source-editor');
                const linked = report.contracts.flatMap(contract => contract.findings).find(finding => finding.location)?.location; const sourceLines = (await page.locator('#source-editor').inputValue()).split('\n'); assert.equal(await page.evaluate(() => { const editor = document.querySelector('#source-editor'); return editor.value.slice(editor.selectionStart, editor.selectionEnd); }), sourceLines[linked.line - 1]);
                results.push('finding links focus and select the affected source line');
            }
            results.push(`${story} ${state}: real browser/native findings and witness parity`);
        }
    }
    const coverageSource = await page.locator('#source-editor').inputValue(), coverageConfig = JSON.parse(await page.locator('#contract-editor').inputValue());
    for (const mutation of [
        { source: '\ndef _private_note():\n    return "internal"\n', surfaces: ['pipeline:_private_note'] },
        { source: '\nif True:\n    def _conditional_note():\n        return "conditional"\n', surfaces: ['pipeline:_conditional_note'] },
        { source: '\ndef aggregate_counties():\n    return 1\n', surfaces: ['pipeline:aggregate_counties'] },
        { source: '\nclass DraftSnapshot(BaseModel):\n    county: str\n', surfaces: ['pipeline:DraftSnapshot'] },
        { source: '\nclass InternalInspector:\n    def probe(self):\n        return None\n', surfaces: ['pipeline:InternalInspector', 'pipeline:InternalInspector.probe'] },
    ]) {
        await page.locator('#source-editor').fill(coverageSource + mutation.source); await page.locator('#contract-editor').fill(JSON.stringify(coverageConfig));
        assert.match(await page.locator('#report-content .verdict').first().innerText(), /STALE/);
        const unclassified = await runReport(); assert.equal(unclassified.status, 'FAIL');
        const inventory = unclassified.contracts.find(contract => contract.kind === 'coverage').coverage;
        for (const identity of mutation.surfaces) {
            const surface = inventory.surfaces.find(item => item.id === identity); assert.ok(surface, `New ${identity} appears in the census`); assert.equal(surface.classification, 'unclassified');
            const item = page.locator('.coverage-inventory li').filter({ has: page.locator('strong', { hasText: identity }) }).first();
            await item.locator('.finding-link').click(); assert.equal(await page.evaluate(() => document.activeElement.id), 'source-editor');
        }
        const classified = structuredClone(coverageConfig); classified.contracts.find(contract => contract.kind === 'coverage').ledger.push(...mutation.surfaces.map(surface => ({ kind: 'internal', surface, reason: 'Deliberately internal to this editable example; it has no public integration requirement.' })));
        await page.locator('#contract-editor').fill(JSON.stringify(classified)); assert.equal((await runReport()).status, 'PASS');
    }
    results.push('new private, conditional, helper, model, class, and method declarations fail coverage until explicitly classified; inventory links focus source');
    const newHelper = 'pipeline:_audit_snapshot', backed = structuredClone(coverageConfig);
    backed.contracts.push({ kind: 'connection', id: 'audit-connected', entry: 'pipeline:main', target: newHelper });
    backed.contracts.find(contract => contract.kind === 'coverage').ledger.push({ kind: 'contract', surface: newHelper, contracts: ['audit-connected'] });
    await page.locator('#source-editor').fill(coverageSource.replace('    validate_snapshot(snapshot)\n', '    validate_snapshot(snapshot)\n    _audit_snapshot(snapshot)\n') + '\ndef _audit_snapshot(snapshot: dict) -> None:\n    return None\n');
    await page.locator('#contract-editor').fill(JSON.stringify(backed)); assert.equal((await runReport()).status, 'PASS'); results.push('a new reachable helper can be deliberately backed by a new integration contract');
    const unsupported = structuredClone(backed); unsupported.contracts.find(contract => contract.kind === 'coverage').ledger.find(entry => entry.surface === newHelper).contracts = ['validator-connected'];
    await page.locator('#contract-editor').fill(JSON.stringify(unsupported)); assert.equal((await runReport()).status, 'ERROR'); results.push('listing an unrelated passing contract is a configuration error');
    await page.locator('#source-editor').fill(coverageSource); const staleLedger = structuredClone(coverageConfig); staleLedger.contracts.find(contract => contract.kind === 'coverage').ledger.push({ kind: 'internal', surface: 'pipeline:_removed', reason: 'A deliberately stale classification.' });
    await page.locator('#contract-editor').fill(JSON.stringify(staleLedger)); const staleInventory = await runReport(); assert.equal(staleInventory.status, 'FAIL'); assert.deepEqual(staleInventory.contracts.find(contract => contract.kind === 'coverage').coverage.stale, ['pipeline:_removed']); assert.match(await page.locator('#report-content').innerText(), /Stale ledger identities/); results.push('removed declarations leave visible stale ledger identities');
    await page.getByRole('button', { name: 'Load repair', exact: true }).click();
    page.once('dialog', dialog => dialog.accept('helpers.py')); await page.getByRole('button', { name: 'Add file', exact: true }).click(); await page.locator('#source-editor').fill('def helper():\n    return 42\n');
    const newFile = await runReport(); assert.equal(newFile.status, 'FAIL'); assert.ok(newFile.contracts.find(contract => contract.kind === 'coverage').coverage.surfaces.some(surface => surface.id === 'helpers:helper' && surface.classification === 'unclassified')); results.push('new files inside the declared root are censused without automatic acceptance');
    await page.getByRole('button', { name: 'Load repair', exact: true }).click(); await runReport();
    const projectDownload = page.waitForEvent('download'); await page.getByRole('button', { name: 'Download project', exact: true }).click();
    const downloadedProject = JSON.parse(await readFile(await (await projectDownload).path(), 'utf8')); assert.equal(downloadedProject.version, 2); assert.equal(downloadedProject.config.version, 2); assert.equal(downloadedProject.files['pipeline.py'], await page.locator('#source-editor').inputValue());
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
    const strictConfig = JSON.parse(await page.locator('#contract-editor').inputValue()); strictConfig.unexpected = 'USER_INPUT_VALUE_MUST_NOT_BE_ECHOED'; await page.locator('#contract-editor').fill(JSON.stringify(strictConfig)); await page.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('#run').disabled); const strictReport = JSON.parse(await page.locator('#report-json').textContent()); assert.equal(strictReport.status, 'ERROR'); assert.equal(await page.locator('#report-content .verdict').first().innerText(), 'ERROR'); assert.equal(await page.locator('#download-report').isDisabled(), false); const errorText = await page.locator('#report-content').innerText(); assert.match(errorText, /config.unexpected: Extra inputs/); assert.doesNotMatch(errorText, /Traceback|input_value|USER_INPUT_VALUE_MUST_NOT_BE_ECHOED|\"code\"/); const errorDownload = page.waitForEvent('download'); await page.getByRole('button', { name: 'Download report', exact: true }).click(); assert.equal(JSON.parse(await readFile(await (await errorDownload).path(), 'utf8')).status, 'ERROR'); results.push('strict browser configuration produces a fresh downloadable ERROR without stack or input echo'); await page.getByRole('button', { name: 'Load repair', exact: true }).click();
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
        await site.locator('[data-sentinels-activate]').click();
        const iframe = site.locator('[data-sentinels-frame] iframe'), frame = site.frameLocator('[data-sentinels-frame] iframe');
        const publishedManifest = await assertReleasedWorkspace(siteContext, iframe, site.url());
        await assertProvenance(frame, publishedManifest);
        assert.equal(siteRequests.filter(url => /runtime\/|\.whl|\.wasm/.test(url)).length, 0, 'Embedded runtime waits for its activation');
        await runWorkspace(frame);
        results.push('article activation, DNT/GPC independence, exact release provenance, scoped headers, and embedded visitor-generated PASS');
        let previous;
        if (process.env.SENTINELS_PREVIOUS_LAB_URL) {
            const previousURL = new URL(process.env.SENTINELS_PREVIOUS_LAB_URL, site.url());
            assert.equal(previousURL.origin, new URL(site.url()).origin, 'Retained workspace is served by the same site');
            assert.match(previousURL.pathname, /^\/labs\/sentinels\/_bundles\/[a-f0-9]{64}\/$/, 'Retained workspace has an immutable bundle URL');
            const oldManifest = await (await siteContext.request.get(new URL('manifest.json', previousURL).href)).json();
            assert.notEqual(oldManifest.wheel.sha256, publishedManifest.wheel.sha256, 'Isolation exercise uses two different released wheels');
            const oldPage = await siteContext.newPage(); await oldPage.goto(previousURL.href); await runWorkspace(oldPage);
            const edited = (await oldPage.locator('#source-editor').inputValue()) + '\n# Keep this edit in the older open workspace.\n'; await oldPage.locator('#source-editor').fill(edited);
            assert.match(await oldPage.locator('#report-content .verdict').first().innerText(), /STALE/);
            previous = { page: oldPage, manifest: oldManifest, edited, url: previousURL.href };
        }
        const labURL = new URL('/labs/sentinels/', process.env.SENTINELS_PORTFOLIO_URL);
        const direct = await siteContext.newPage(), directRequests = []; direct.on('request', request => directRequests.push(request.url()));
        const shellResponse = await direct.goto(labURL.href); await direct.waitForLoadState('networkidle');
        assert.equal(direct.url(), labURL.href, 'Public lab URL has no release version');
        assert.ok(!(await shellResponse.allHeaders())['content-security-policy'].includes('wasm-unsafe-eval'), 'Stable shell keeps the main site policy');
        const directIframe = direct.locator('[data-sentinels-workspace] iframe'), directFrame = direct.frameLocator('[data-sentinels-workspace] iframe');
        const directManifest = await assertReleasedWorkspace(siteContext, directIframe, direct.url()); await assertProvenance(directFrame, directManifest);
        assert.equal(directRequests.filter(url => /runtime\/|\.whl|\.wasm/.test(url)).length, 0, 'Direct-open runtime waits for activation');
        await runWorkspace(directFrame); results.push('stable direct-open shell retains immutable workspace and generates a fresh PASS');
        if (previous) {
            assert.equal(previous.page.url(), previous.url); assert.equal(await previous.page.locator('#source-editor').inputValue(), previous.edited); assert.match(await previous.page.locator('#report-content .verdict').first().innerText(), /STALE/);
            await siteContext.setOffline(true);
            try {
                await previous.page.getByRole('button', { name: 'Run fresh analysis', exact: true }).click(); await previous.page.locator('#run:not([disabled])').waitFor({ timeout: 7000 });
                const oldReport = JSON.parse(await previous.page.locator('#report-json').textContent()); assert.equal(oldReport.status, 'PASS'); assert.equal(oldReport.checker_version, previous.manifest.version);
                assert.notEqual(oldReport.checker_version, directManifest.version); assert.equal(await previous.page.locator('#source-editor').inputValue(), previous.edited);
            } finally { await siteContext.setOffline(false); }
            results.push('retained old workspace keeps edits, stale status, and its own released checker while the stable shell opens the current release');
        }
        for (const path of ['/labs/sentinels/v0.1.0/', '/labs/sentinels/v0.1.0', '/labs/sentinels/v0.1.0/index.html']) {
            const legacy = await siteContext.request.get(new URL(path, labURL).href, { maxRedirects: 0 });
            assert.ok([301, 302, 307, 308].includes(legacy.status()), `Legacy landing ${path} redirects`);
            assert.equal(new URL(legacy.headers().location, labURL).href, labURL.href);
        }
        results.push('legacy release landing pages redirect to the stable lab URL');
        await siteContext.close();
    }
    await mkdir(resolve(root, 'dist'), { recursive: true }); await writeFile(resolve(root, 'dist/browser-qualification.json'), JSON.stringify({ tests: results, parser_version: JSON.parse(await page.locator('#report-json').textContent()).parser_version, checker_version: JSON.parse(await page.locator('#report-json').textContent()).checker_version, pyodide: '314.0.7', browser: await browser.version(), wheel_manifest: JSON.parse(await readFile(resolve(directory, 'manifest.json'), 'utf8')), fixture_cases: storyIds.length * 2 + corpusResults.length }, null, 2) + '\n');
    console.log(JSON.stringify({ passed: results.length, results }, null, 2));
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
