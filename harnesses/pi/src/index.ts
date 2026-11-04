import { readFileSync } from 'node:fs';
import { serveStdio } from '@evalhive/harness-sdk';
import { PiConnector } from './connector.js';

const path = process.env.EVALHIVE_CREDENTIALS_FILE;
await serveStdio(new PiConnector(path ? JSON.parse(readFileSync(path, 'utf8')) : {}));
