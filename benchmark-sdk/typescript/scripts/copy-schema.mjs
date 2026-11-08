import {mkdirSync, copyFileSync} from 'node:fs';
const destination = new URL('../dist/schemas/', import.meta.url);
mkdirSync(destination, {recursive: true});
copyFileSync(new URL('../../python/src/evalhive_benchmark/schemas/contract.json', import.meta.url),
  new URL('contract.json', destination));
