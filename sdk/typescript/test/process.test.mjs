import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {ConnectorClient} from '../dist/index.js';

const fixtures = JSON.parse(readFileSync(new URL('../../fixtures/conformance.json', import.meta.url), 'utf8'));
const example = fileURLToPath(new URL('../../examples/fake/', import.meta.url));
async function untilFinished(client) {
  const events = [];
  for await (const event of client.events()) {
    events.push(event);
    if (event.event === 'run.finished') break;
  }
  assert.deepEqual(events.map(e => e.seq), events.map((_, i) => i));
  assert.equal(new Set(events.map(e => e.event_id)).size, events.length);
  assert.equal(events[0].event, 'run.started');
  return events;
}

for (const language of ['typescript', 'python']) for (const mode of ['success', 'failure', 'abort', 'shutdown'])
  test(`${language} connector: ${mode}`, {timeout: 15000}, async () => {
    const argv = language === 'typescript' ? ['node', example + 'connector.mjs'] : [process.env.EH_TEST_PYTHON ?? 'python3', example + 'connector.py'];
    const client = ConnectorClient.launch(argv), run = structuredClone(fixtures.run_spec);
    Object.assign(run.config, {wait: ['abort', 'shutdown'].includes(mode), fail: mode === 'failure'});
    try {
      assert.equal((await client.request('describe')).id, 'example.fake');
      assert.equal((await client.request('validate', {run})).accepted, true);
      assert.equal((await client.request('start', {run})).accepted, true);
      if (['abort', 'shutdown'].includes(mode)) {
        await assert.rejects(client.request('control', {attempt_id: 'old', command: 'abort'}), {code: 'ATTEMPT_NOT_ACTIVE'});
        await assert.rejects(client.request('control', {attempt_id: run.attempt_id, command: 'approval'}), {code: 'UNSUPPORTED_CAPABILITY'});
        await assert.rejects(client.request('control', {attempt_id: run.attempt_id, command: 'feedback', payload: {text: 2}}), {code: 'INVALID_CONFIG'});
        await client.request('control', {attempt_id: run.attempt_id, command: 'feedback', payload: {text: 'Continue'}});
      }
      if (mode === 'abort') await client.request('control', {attempt_id: run.attempt_id, command: 'abort'});
      if (mode === 'shutdown') await client.close();
      const events = await untilFinished(client);
      assert.equal(events.at(-1).data.status, {success: 'success', failure: 'failed', abort: 'cancelled', shutdown: 'cancelled'}[mode]);
      assert.equal(JSON.stringify(events).includes('Internal diagnostics'), false);
      if (mode === 'success') assert(events.some(e => e.event === 'artifact'));
      if (mode !== 'shutdown') await assert.rejects(client.request('start', {run}), {code: 'ALREADY_STARTED'});
    } finally { await client.close(); }
    assert.equal(client.process.exitCode, 0);
  });

for (const [output, error] of [['{}', 'INVALID_FRAME'], ['', 'TRANSPORT_CLOSED']])
  test(`broken stream: ${error}`, async () => {
    const script = `process.stdin.once('data',()=>{process.stdout.end(${JSON.stringify(output)}); process.stdin.destroy();});`;
    const client = ConnectorClient.launch(['node', '-e', script]);
    try {
      await assert.rejects(client.request('describe', {}, 2000), {code: error});
      await assert.rejects(async () => {for await (const _ of client.events()) {}}, {code: error});
    } finally { await client.close(200); }
  });

for (const mode of ['reorder', 'late', 'flood', 'hang']) test(`adversarial wire peer: ${mode}`, {timeout: 10000}, async () => {
  const client = ConnectorClient.launch([process.env.EH_TEST_PYTHON ?? 'python3',
    fileURLToPath(new URL('../../fixtures/peer.py', import.meta.url)), mode], {eventBuffer: 1, maxPending: mode === 'hang' ? 1 : 64});
  try {
    if (mode === 'reorder') {
      const [first, second] = await Promise.all([client.request('shutdown'), client.request('describe')]);
      assert.deepEqual(first, {method: 'shutdown'}); assert.equal(second.id, 'example.fake');
    } else if (mode === 'late') {
      await assert.rejects(client.request('shutdown', {}, 50), {code: 'TIMEOUT'});
      assert.equal((await client.request('describe')).id, 'example.fake');
    } else if (mode === 'flood') {
      await assert.rejects(client.request('describe'), {code: 'EVENT_BUFFER_FULL'});
      const received = [];
      await assert.rejects(async () => {for await (const event of client.events()) received.push(event);}, {code: 'EVENT_BUFFER_FULL'});
      assert.equal(received.length, 1);
    } else {
      const pending = client.request('describe', {}, 200);
      const checked = assert.rejects(pending, {code: 'TIMEOUT'});
      await assert.rejects(client.request('describe'), {code: 'PENDING_LIMIT'});
      await checked;
    }
  } finally { await client.close(100); }
});
