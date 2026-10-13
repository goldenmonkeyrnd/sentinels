export interface ReleaseManifest {
    version: string;
    runtime: string;
    wheel: { file: string; sha256: string };
    release: { tag: string; url: string; source_url: string; source_archive_url: string };
    source_commit?: string;
    source_dirty?: boolean;
    schema_version?: number;
}

function githubLink(value: unknown): string {
    if (typeof value !== 'string') throw new Error('Release provenance is missing a source link.');
    const url = new URL(value);
    if (url.origin !== 'https://github.com' || !url.pathname.startsWith('/goldenmonkeyrnd/sentinels/') || url.username || url.password || url.search || url.hash) throw new Error('Release provenance contains an invalid source link.');
    return url.href;
}

export async function loadManifest(): Promise<ReleaseManifest> {
    const response = await fetch(new URL('./manifest.json', import.meta.url));
    if (!response.ok) throw new Error(`Release provenance failed to load (${response.status}).`);
    const manifest = await response.json() as ReleaseManifest;
    if (!manifest || typeof manifest.version !== 'string' || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(manifest.version) || manifest.release?.tag !== `v${manifest.version}`) throw new Error('Release provenance has an invalid version or tag.');
    if (typeof manifest.runtime !== 'string' || !manifest.wheel || typeof manifest.wheel.file !== 'string' || !/^[a-zA-Z0-9_.-]+\.whl$/.test(manifest.wheel.file) || !/^[a-f0-9]{64}$/.test(manifest.wheel.sha256)) throw new Error('Release provenance has an invalid runtime or wheel identity.');
    manifest.release.url = githubLink(manifest.release.url);
    manifest.release.source_url = githubLink(manifest.release.source_url);
    manifest.release.source_archive_url = githubLink(manifest.release.source_archive_url);
    if (new URL(manifest.release.url).pathname !== `/goldenmonkeyrnd/sentinels/releases/tag/${manifest.release.tag}` || !new URL(manifest.release.source_archive_url).pathname.startsWith(`/goldenmonkeyrnd/sentinels/releases/download/${manifest.release.tag}/`)) throw new Error('Release links do not match the bundled version.');
    if (manifest.source_commit !== undefined && !/^[a-f0-9]{40}$/.test(manifest.source_commit)) throw new Error('Release provenance has an invalid source commit.');
    const sourceTargets = [manifest.release.tag, manifest.source_commit].filter(Boolean).map(ref => `/goldenmonkeyrnd/sentinels/tree/${ref}`);
    if (!sourceTargets.includes(new URL(manifest.release.source_url).pathname)) throw new Error('Source link does not match the bundled release or source commit.');
    if (manifest.source_dirty !== undefined && typeof manifest.source_dirty !== 'boolean') throw new Error('Release provenance has an invalid source state.');
    if (manifest.schema_version !== undefined && (!Number.isInteger(manifest.schema_version) || manifest.schema_version < 1)) throw new Error('Release provenance has an invalid schema version.');
    return manifest;
}
