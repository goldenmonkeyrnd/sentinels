import { stories } from '../lab/stories';
import { mkdir } from 'node:fs/promises';
await mkdir(new URL('../examples/', import.meta.url), { recursive: true });
for (const story of stories) {
    for (const state of ['broken', 'repaired'] as const) {
        await Bun.write(new URL(`../examples/${story.id}-${state}.json`, import.meta.url), JSON.stringify(story[state], null, 2) + '\n');
    }
}
