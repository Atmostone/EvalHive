import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {exerciseConversation, exerciseExternalTools, exerciseExternalRounds, exerciseCancellation, exerciseNativeFailure, exercisePreflight} from '../../shared/conversation.mjs';
import {stream} from '../../shared/fake-openai.mjs';
const entrypoint = fileURLToPath(new URL('../dist/index.js', import.meta.url));
test('native Pi preserves a seeded session over two explicit turns', {timeout: 40000}, () => exerciseConversation(entrypoint, stream));
for (const cancel of [false, true]) test(`native Pi groups externally owned tools; cancel=${cancel}`, {timeout: 40000},
  () => exerciseExternalTools(entrypoint, stream, {cancel}));
for (const generating of [false, true]) test(`native Pi conversation abort; generating=${generating}`, {timeout: 40000},
  () => exerciseCancellation(entrypoint, {generating}));
test('native Pi conversation failure ends the attempt', {timeout: 40000}, () => exerciseNativeFailure(entrypoint));
test('Pi rejects unsupported external tools and invalid history before execution', {timeout: 20000}, () => exercisePreflight(entrypoint));
test('Pi preserves a driver tool error in native history and normalized evidence', {timeout: 40000},
  () => exerciseExternalTools(entrypoint, stream, {error: true}));
test('Pi completes consecutive external batches within one user turn', {timeout: 40000}, () => exerciseExternalRounds(entrypoint, stream));
