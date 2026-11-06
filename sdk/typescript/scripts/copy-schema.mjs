import { copyFile, mkdir } from 'node:fs/promises';

// The Python package owns the single committed schema; npm gets a build copy.
const source = new URL('../../python/src/evalhive_harness/schemas/contract.json', import.meta.url);
const target = new URL('../dist/schemas/', import.meta.url);
await mkdir(target, { recursive: true });
await copyFile(source, new URL('contract.json', target));
