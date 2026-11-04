import { readFileSync } from 'node:fs';
import { serveStdio } from '@evalhive/harness-sdk';
import { OpenCodeConnector } from './connector.js';

const path = process.env.EVALHIVE_CREDENTIALS_FILE;
const credentials = path ? (JSON.parse(readFileSync(path, 'utf8')) as Record<string, string>) : {};
await serveStdio(new OpenCodeConnector(credentials));
