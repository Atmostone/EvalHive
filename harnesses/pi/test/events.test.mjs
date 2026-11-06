import assert from 'node:assert/strict';
import test from 'node:test';
import { EventQueue, UsageTracker, decimal } from '../dist/events.js';
import { failureFacts } from '../dist/errors.js';
import { connection, effectiveConfig } from '../dist/config.js';

test('common sampling is translated and unsupported settings fail before execution', () => {
  const spec = {protocol_version: '1.0', workspace_id: 'w', task_id: 't', attempt_id: 'a',
    task: {prompt: 'Fixture'}, cwd: '/workspace', config: {temperature: 1},
    model: {sampling: {temperature: 0, seed: 42}}};
  assert.equal(effectiveConfig(spec).temperature, 0);
  assert.equal(effectiveConfig(spec).seed, 42);
  for (const sampling of [null, [], {ignoredOption: true}, {temperature: 3}])
    assert.throws(() => effectiveConfig({...spec, model: {sampling}}));
});

function records() {
  const events = [];
  let aborted = false;
  const queue = new EventQueue({emit: async (name, data) => { events.push({name, data}); }}, 'session', () => { aborted = true; });
  return {queue, events, aborted: () => aborted};
}

test('raw provider availability preserves known zeros and unknown fields', async () => {
  const {queue, events} = records();
  const usage = new UsageTracker(queue, 'fake');
  usage.begin('agent');
  usage.provider({usage: {prompt_tokens: 20, completion_tokens: 3, prompt_tokens_details: {cached_tokens: 0}}}, 'fake');
  usage.assistant({input: 20, output: 3, cost: {total: 0}});
  await queue.drain();
  const value = events.at(-1).data;
  assert.equal(value.input_tokens, 20);
  assert.equal(value.cache_read_tokens, 0);
  assert.equal(value.cache_write_tokens, null);
  assert.equal(value.reasoning_tokens, null);
  assert.equal(value.cost, null);
  assert.equal(new Set(events.map((event) => event.data.usage_id)).size, 1);
});

test('provider billed cost stays distinct from SDK price defaults', async () => {
  const {queue, events} = records();
  const usage = new UsageTracker(queue, 'fake');
  usage.begin('agent');
  usage.assistant({cost: {total: 1e-8, totalFromProvider: true}});
  await queue.drain();
  assert.deepEqual(events.at(-1).data.cost, {amount: '0.00000001', currency: 'USD', source: 'provider'});
  assert.equal(decimal(1e21), '1000000000000000000000');
});

test('event buffering is bounded and triggers native abort', async () => {
  const {queue, aborted} = records();
  queue.emit('message.end', {message_id: 'large', content: 'x'.repeat(9 * 1024 * 1024)});
  assert(aborted());
  await assert.rejects(queue.drain(), {code: 'EVENT_QUEUE_FULL'});
});

test('Pi failure syntax becomes SDK facts', () => {
  assert.deepEqual(failureFacts('error', '401: expected error'), {kind: 'model_error', status_code: 401, exception: null});
  assert.deepEqual(failureFacts('error', 'Connection error.'), {kind: 'model_error', status_code: null, exception: 'APIConnectionError'});
  assert.deepEqual(failureFacts('length', ''), {kind: 'limit'});
  assert.deepEqual(failureFacts('error', 'Stream ended without finish_reason', 200), {kind: 'recording_error'});
});

test('preflight rejects unsupported settings and inline endpoint credentials', () => {
  const spec = {protocol_version: '1.0', task_id: 't', attempt_id: 'a', workspace_id: 'w',
    task: {prompt: 'Test'}, cwd: '/workspace', config: {}};
  assert.equal(effectiveConfig(spec).thinkingLevel, 'off');
  assert.throws(() => effectiveConfig({...spec, config: {packages: ['unapproved-code']}}), {code: 'INVALID_CONFIG'});
  assert.throws(() => connection(spec, {}), {code: 'MODEL_REQUIRED'});
  assert.throws(() => connection({...spec, model: {name: 'fake', endpoint: 'https://user:secret@example.invalid', credential_ref: 'k'}}, {k: 'fake'}), {code: 'INVALID_MODEL_ENDPOINT'});
});

test('resource preflight rejects unavailable native tools and credentials', async () => {
  const {resourcePlan} = await import('../dist/resources.js');
  const spec = {cwd: '/workspace', environment: {id: 'fixture', resources: [
    {id: 'tools', kind: 'tools', config: {names: ['invented_tool']}}]}};
  assert.throws(() => resourcePlan(spec, {}), {code: 'UNSUPPORTED_TOOL'});
  spec.environment.resources = [{id: 'mcp', kind: 'mcp', config: {transport: 'http', url: 'http://fixture/mcp'},
    credential_refs: {'headers.Authorization': 'missing'}}];
  assert.throws(() => resourcePlan(spec, {}), {code: 'CREDENTIAL_REQUIRED'});
});

test('long MCP names are deterministic, model-safe and collision resistant', async () => {
  const {modelToolName} = await import('../dist/resources.js');
  const prefix = 'mcp__fixture__' + 'long'.repeat(30);
  assert.equal(modelToolName(prefix), modelToolName(prefix));
  assert.match(modelToolName(prefix), /^[A-Za-z0-9_-]{64}$/);
  assert.notEqual(modelToolName(prefix + 'a'), modelToolName(prefix + 'b'));
});
