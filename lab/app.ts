import { stories, type ProjectBundle } from './stories';
interface Location { path: string; line: number; column?: number }
interface Edge { source: string; target: string; kind: string; location?: Location; detail?: string }
interface Finding { code: string; message: string; location?: Location | null }
interface ContractResult { id: string; kind: string; status: string; findings: Finding[]; witness: Edge[]; unresolved: string[] }
interface Report { status: string; scope: string[]; contracts: ContractResult[]; edges: Edge[]; assumptions: string[]; errors: Finding[]; checker_version?: string; parser_version?: string }
const get = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const source = get<HTMLTextAreaElement>('source-editor'), config = get<HTMLTextAreaElement>('contract-editor'), fileSelect = get<HTMLSelectElement>('file-select');
const activate = get<HTMLButtonElement>('activate'), run = get<HTMLButtonElement>('run'), cancel = get<HTMLButtonElement>('cancel');
const runtimeStatus = get('runtime-status'), runStatus = get('run-status'), reportContent = get('report-content');
const reportDownload = get<HTMLButtonElement>('download-report');
let currentStory = stories[0], files: Record<string, string> = {}, selectedFile = '', worker: Worker | undefined;
let ready = false, busy = false, sequence = 0, pending = 0, timeout: ReturnType<typeof setTimeout> | undefined, lastReport: Report | undefined, stale = false;
const utf8 = new TextEncoder();
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)); }
function node<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] { const result = document.createElement(tag); if (text !== undefined) result.textContent = text; if (className) result.className = className; return result; }
function saveSource() { if (selectedFile) files[selectedFile] = source.value; }
function renderFiles(preferred?: string) {
    fileSelect.replaceChildren();
    for (const path of Object.keys(files).sort()) { const option = node('option', path); option.value = path; fileSelect.append(option); }
    selectedFile = preferred && preferred in files ? preferred : Object.keys(files).sort()[0];
    fileSelect.value = selectedFile; source.value = files[selectedFile] ?? '';
}
function markStale() {
    if (busy) runStatus.textContent = 'Input changed during analysis. The pending result will be marked stale.';
    if (lastReport || busy) stale = true;
    if (lastReport) { const status = reportContent.querySelector('.verdict'); if (status) { status.textContent = `STALE · ${lastReport.status}`; status.className = 'verdict stale'; } }
    reportDownload.disabled = true;
}
function loadProject(project: ProjectBundle) {
    files = clone(project.files); config.value = JSON.stringify(project.config, null, 2); renderFiles();
    markStale();
    if (busy) stop('The project changed. Analysis was canceled; your edits are preserved.');
}
function chooseStory(id: string) {
    currentStory = stories.find(story => story.id === id) ?? stories[0];
    get('story-summary').textContent = currentStory.summary; get('story-question').textContent = currentStory.question;
    for (const button of get('story-list').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.id === id));
    loadProject(currentStory.broken);
}
for (const story of stories) { const button = node('button', story.title); button.dataset.id = story.id; button.addEventListener('click', () => chooseStory(story.id)); get('story-list').append(button); }
chooseStory(currentStory.id);
source.addEventListener('input', () => { saveSource(); markStale(); }); config.addEventListener('input', markStale);
fileSelect.addEventListener('change', () => { saveSource(); selectedFile = fileSelect.value; source.value = files[selectedFile]; });
get('broken').addEventListener('click', () => loadProject(currentStory.broken)); get('repair').addEventListener('click', () => loadProject(currentStory.repaired));
get('add-file').addEventListener('click', () => {
    const path = window.prompt('Relative source path (for example helpers.py):'); if (path === null) return;
    if (!path || path.startsWith('/') || path.includes('\\') || path.split('/').some(part => !part || part === '.' || part === '..') || !path.endsWith('.py')) { runStatus.textContent = 'Use a relative .py path without empty, dot, or parent segments.'; return; }
    if (path in files) { runStatus.textContent = 'That file already exists.'; return; }
    if (Object.keys(files).length >= 20) { runStatus.textContent = 'This project already has 20 files.'; return; }
    saveSource(); files[path] = ''; renderFiles(path); markStale(); source.focus();
});
get('remove-file').addEventListener('click', () => { if (Object.keys(files).length <= 1) { runStatus.textContent = 'Keep at least one source file.'; return; } delete files[selectedFile]; renderFiles(); markStale(); });
function validateLimits(bundle: ProjectBundle) {
    if (!bundle.files || typeof bundle.files !== 'object' || Array.isArray(bundle.files)) throw new Error('Project files must be a mapping of paths to source text.');
    const entries = Object.entries(bundle.files); if (!entries.length || entries.length > 20) throw new Error('A browser project must contain between 1 and 20 files.');
    let total = 0;
    for (const [path, content] of entries) { if (typeof content !== 'string') throw new Error(`${path}: source must be a string.`); const size = utf8.encode(content).length; if (size > 16 * 1024) throw new Error(`${path} exceeds 16 KiB.`); total += size; }
    if (total > 100 * 1024) throw new Error('The project exceeds 100 KiB total source.');
}
function bundle(): ProjectBundle { saveSource(); const result: ProjectBundle = { version: 1, files: clone(files), config: JSON.parse(config.value) }; validateLimits(result); return result; }
function download(name: string, value: unknown) { const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' })); const link = node('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 500); }
get('download-project').addEventListener('click', () => { try { download('sentinels-project.json', bundle()); } catch (error) { runStatus.textContent = String(error); } });
reportDownload.addEventListener('click', () => { if (lastReport && !stale) download('sentinels-report.json', lastReport); });
get<HTMLInputElement>('upload-project').addEventListener('change', async (event) => {
    const input = event.target as HTMLInputElement, file = input.files?.[0]; if (!file) return;
    try { if (file.size > 512 * 1024) throw new Error('Project bundle exceeds 512 KiB.'); const project = JSON.parse(await file.text()); if (project.version !== 1 || !project.config || typeof project.config !== 'object' || Object.keys(project).some(key => !['version', 'files', 'config'].includes(key))) throw new Error('Use a version 1 project bundle with files and config.'); validateLimits(project); loadProject(project); runStatus.textContent = 'Project opened. Run the checker to validate its configuration.'; } catch (error) { runStatus.textContent = String(error); } finally { input.value = ''; }
});
function stop(message: string) {
    clearTimeout(timeout); worker?.terminate(); worker = undefined; ready = false; busy = false; pending = ++sequence;
    run.disabled = true; cancel.disabled = true; activate.disabled = false; activate.textContent = 'Initialize checker again'; runtimeStatus.textContent = 'Worker stopped. Your project remains in these editors.'; runStatus.textContent = message;
}
function initialize() {
    if (busy || ready) return;
    worker?.terminate(); worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' }); busy = true; pending = ++sequence;
    activate.disabled = true; cancel.disabled = false; runtimeStatus.textContent = 'Loading the self-hosted Python runtime and verified checker. This may take a moment.'; runStatus.textContent = 'Initializing…';
    timeout = setTimeout(() => stop('Initialization exceeded 30 seconds. Try again; your edits are preserved.'), 30_000);
    worker.onerror = (event) => stop(`Worker error: ${event.message || 'Initialization failed'}`);
    worker.onmessage = (event: MessageEvent) => {
        const message = event.data; if (message.id !== pending) return;
        clearTimeout(timeout); busy = false; cancel.disabled = true;
        if (message.kind === 'ready') { ready = true; run.disabled = false; activate.textContent = 'Checker ready'; runtimeStatus.textContent = 'Ready. The same released Python wheel runs locally in this worker. You can now analyze offline.'; runStatus.textContent = 'Choose a defect or edit the source, then run fresh analysis.'; }
        else if (message.kind === 'report') { run.disabled = false; lastReport = message.report; renderReport(lastReport!); reportDownload.disabled = stale; runStatus.textContent = stale ? 'Analysis finished for an earlier input. Run again for your current edits.' : `Fresh analysis finished: ${lastReport!.status}.`; }
        else if (message.kind === 'error') { if (!ready) stop(`Initialization failed: ${message.error}`); else { run.disabled = false; markStale(); runStatus.textContent = `ERROR: ${message.error}`; reportDownload.disabled = true; } }
    };
    worker.postMessage({ id: pending, kind: 'initialize' });
}
activate.addEventListener('click', initialize); cancel.addEventListener('click', () => stop('Analysis canceled. Initialize again to run; your edits are preserved.'));
run.addEventListener('click', () => {
    if (!ready || busy || !worker) return;
    try { const project = bundle(); stale = false; busy = true; pending = ++sequence; run.disabled = true; cancel.disabled = false; reportDownload.disabled = true; runStatus.textContent = 'Analyzing your current source as data…'; timeout = setTimeout(() => stop('Analysis exceeded five seconds. The worker was stopped; your edits are preserved.'), 5_000); worker.postMessage({ id: pending, kind: 'analyze', bundle: project }); }
    catch (error) { runStatus.textContent = `ERROR: ${error instanceof Error ? error.message : String(error)}`; }
});
function jump(location: Location) {
    if (!(location.path in files)) { runStatus.textContent = `Location ${location.path}:${location.line} is outside the editable files.`; return; }
    saveSource(); renderFiles(location.path); source.focus(); const lines = source.value.split('\n'); const start = lines.slice(0, Math.max(0, location.line - 1)).reduce((length, line) => length + line.length + 1, 0); const end = start + (lines[location.line - 1]?.length ?? 0); source.setSelectionRange(start, end); source.scrollTop = Math.max(0, (location.line - 4) * 22); source.scrollIntoView({ behavior: 'smooth', block: 'center' });
}
function sourceLink(location: Location, label?: string) { const button = node('button', label ?? `${location.path}:${location.line}`, 'finding-link'); button.addEventListener('click', () => jump(location)); return button; }
function renderReport(report: Report) {
    reportContent.replaceChildren(); const verdict = node('p', stale ? `STALE · ${report.status}` : report.status, `verdict ${stale ? 'stale' : report.status.toLowerCase()}`); reportContent.append(verdict);
    reportContent.append(node('p', `Checker ${report.checker_version ?? 'version in report'} · parser ${report.parser_version ?? 'version in report'}. Inspected scope: ${report.scope.length ? report.scope.join(', ') : 'none'}.`));
    for (const error of report.errors ?? []) { const message = node('p', `ERROR: ${error.message}`); if (error.location) message.append(' ', sourceLink(error.location)); reportContent.append(message); }
    for (const contract of report.contracts ?? []) {
        const section = node('article', undefined, 'contract'); section.append(node('h3', `${contract.id} · ${contract.kind}`), node('p', contract.status, `verdict ${contract.status.toLowerCase()}`));
        for (const finding of contract.findings ?? []) { const p = node('p', `${finding.code}: ${finding.message} `); if (finding.location) p.append(sourceLink(finding.location)); section.append(p); }
        for (const reason of contract.unresolved ?? []) section.append(node('p', `Unresolved: ${reason}`));
        if (contract.witness?.length) { const list = node('ol'); for (const edge of contract.witness) { const li = node('li', `${edge.source} → ${edge.target} (${edge.kind}) `); if (edge.location) li.append(sourceLink(edge.location)); if (edge.detail) li.append(node('span', ` — ${edge.detail}`)); list.append(li); } section.append(list); }
        else if (contract.status === 'PASS') section.append(node('p', 'The declared shape was supported by the inspected source or artifact. See the full report for fields and scope.'));
        reportContent.append(section);
    }
    if (report.assumptions?.length) { const details = node('details'); details.append(node('summary', 'Adapter assumptions and analysis boundary')); const list = node('ul'); for (const assumption of report.assumptions) list.append(node('li', assumption)); details.append(list); reportContent.append(details); }
    get('report-json').textContent = JSON.stringify(report, null, 2); renderGraph(report.edges ?? []);
}
function renderGraph(edges: Edge[]) {
    const text = get('graph-text'); text.replaceChildren();
    for (const edge of edges) { const li = node('li', `${edge.source} → ${edge.target} · ${edge.kind} `); if (edge.location) li.append(sourceLink(edge.location)); text.append(li); }
    const container = get('graph-svg'); container.replaceChildren(); if (!edges.length) { text.append(node('li', 'No supported graph edges in this report.')); return; }
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), height = Math.min(edges.length, 20) * 70 + 20; svg.setAttribute('viewBox', `0 0 900 ${height}`); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Supported connections. The following list provides source links and all edges.');
    const add = (tag: string, attrs: Record<string, string>, value?: string) => { const element = document.createElementNS(ns, tag); for (const [key, val] of Object.entries(attrs)) element.setAttribute(key, val); if (value !== undefined) element.textContent = value; svg.append(element); return element; };
    edges.slice(0, 20).forEach((edge, index) => { const y = index * 70 + 18; add('rect', { x: '5', y: String(y), width: '345', height: '43', rx: '5', fill: '#e7f1e8', stroke: '#c9d0c6' }); add('rect', { x: '550', y: String(y), width: '345', height: '43', rx: '5', fill: '#e7f1e8', stroke: '#c9d0c6' }); add('text', { x: '15', y: String(y + 27), fill: '#222c29', 'font-size': '13' }, edge.source); add('text', { x: '560', y: String(y + 27), fill: '#222c29', 'font-size': '13' }, edge.target); add('line', { x1: '353', y1: String(y + 22), x2: '545', y2: String(y + 22), stroke: '#156f56', 'stroke-width': '2' }); add('text', { x: '450', y: String(y + 16), fill: '#156f56', 'font-size': '12', 'text-anchor': 'middle' }, edge.kind); add('path', { d: `M 537 ${y + 16} L 545 ${y + 22} L 537 ${y + 28}`, fill: 'none', stroke: '#156f56', 'stroke-width': '2' }); });
    container.append(svg); if (edges.length > 20) container.append(node('p', 'The diagram shows the first 20 edges; the textual list includes every edge.'));
}
