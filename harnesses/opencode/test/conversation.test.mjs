import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {exerciseConversation, exerciseExternalTools, exerciseExternalRounds, exerciseCancellation, exerciseNativeFailure, exercisePreflight} from '../../shared/conversation.mjs';
import {stream} from '../../shared/fake-openai.mjs';
const entrypoint = fileURLToPath(new URL('../dist/index.js', import.meta.url));
test('native OpenCode preserves a seeded session over two explicit turns', {timeout: 60000}, () => exerciseConversation(entrypoint, stream));
for (const cancel of [false, true]) test(`native OpenCode groups externally owned tools; cancel=${cancel}`, {timeout: 60000},
  () => exerciseExternalTools(entrypoint, stream, {cancel}));
for (const generating of [false, true]) test(`native OpenCode conversation abort; generating=${generating}`, {timeout: 60000},
  () => exerciseCancellation(entrypoint, {generating}));
test('native OpenCode conversation failure ends the attempt', {timeout: 60000}, () => exerciseNativeFailure(entrypoint));
test('OpenCode rejects unsupported external tools and invalid history before execution', {timeout: 20000}, () => exercisePreflight(entrypoint));
test('OpenCode preserves a driver tool error in native history and normalized evidence', {timeout: 60000},
  () => exerciseExternalTools(entrypoint, stream, {error: true}));
test('OpenCode completes consecutive external batches within one user turn', {timeout: 60000}, () => exerciseExternalRounds(entrypoint, stream));
