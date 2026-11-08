import assert from 'node:assert/strict';
import test from 'node:test';
import { EventTranslator, EventWriter } from '../dist/events.js';
import { resourcePlan } from '../dist/resources.js';
import { connection, effectiveConfig } from '../dist/config.js';

test('common sampling is translated and unsupported settings fail before execution', () => {
  const spec = {protocol_version: '1.0', workspace_id: 'w', task_id: 't', attempt_id: 'a',
    task: {prompt: 'Fixture'}, cwd: '/workspace', config: {temperature: 1},
    model: {sampling: {temperature: 0}}};
  assert.equal(effectiveConfig(spec).temperature, 0);
  assert.equal(effectiveConfig(spec).apiTimeoutSeconds, 30);
  assert.equal(effectiveConfig({...spec, config: {apiTimeoutSeconds: 60}}).apiTimeoutSeconds, 60);
  for (const apiTimeoutSeconds of [0, 121, 0.5])
    assert.throws(() => effectiveConfig({...spec, config: {apiTimeoutSeconds}}));
  for (const sampling of [null, [], {seed: 42}, {temperature: 3}])
    assert.throws(() => effectiveConfig({...spec, model: {sampling}}));
});

function recorder() {
  const events = [];
  let stopped = false;
  const writer = new EventWriter(
    { emit: async (name, data, scope) => events.push({ name, data, scope }) },
    () => {
      stopped = true;
    },
  );
  return { writer, events, stopped: () => stopped };
}

test('native replay and interleaved child tools retain stable identities', async () => {
  const { writer, events } = recorder();
  const translator = new EventTranslator(writer, { read: ['file_read'] });
  const event = (id, type, data) => ({ id, type, data });
  translator.event(event('child', 'session.created', { sessionID: 'child', info: { parentID: 'root' } }));
  for (const session of ['root', 'child'])
    translator.event(
      event(`message-${session}`, 'message.updated', {
        sessionID: session,
        info: { id: 'message', sessionID: session, role: 'assistant', time: { created: 1 } },
      }),
    );
  const part = (session, status, output) => ({
    id: 'part',
    sessionID: session,
    messageID: 'message',
    type: 'tool',
    callID: 'same',
    tool: 'read',
    state: {
      status,
      input: { path: session },
      output,
      time: {
        start: session === 'root' ? 100 : 105,
        ...(output ? { end: session === 'root' ? 120 : 110 } : {}),
      },
    },
  });
  const start = event('a', 'message.part.updated', { sessionID: 'root', part: part('root', 'running') });
  translator.event(start);
  translator.event(
    event('b', 'message.part.updated', { sessionID: 'child', part: part('child', 'running') }),
  );
  translator.event(
    event('c', 'message.part.updated', { sessionID: 'child', part: part('child', 'completed', 'B') }),
  );
  translator.event({ ...start, properties: start.data, data: undefined });
  translator.event(
    event('d', 'message.part.updated', { sessionID: 'root', part: part('root', 'completed', 'A') }),
  );
  await writer.drain();
  const calls = events.filter((value) => value.name.startsWith('tool.'));
  assert.equal(calls.length, 4);
  assert.deepEqual(calls[0].data.aliases, ['file_read']);
  assert.equal(calls[1].scope.parent_session_id, 'root');
  assert.equal(calls[2].data.duration_ms, 5);
  assert.equal(calls[3].data.duration_ms, 20);
});

test('nested tools retain the native parent call and portable MCP aliases', async () => {
  const {writer, events} = recorder();
  const translator = new EventTranslator(writer, {}, {m0_fixture: 'fixture__'});
  translator.parents.set('child', 'root');
  translator.snapshot([{info: {id: 'root-message', sessionID: 'root', role: 'assistant'}, parts: [{
    id: 'parent', sessionID: 'root', messageID: 'root-message', type: 'tool', tool: 'task', callID: 'parent-call',
    state: {status: 'running', input: {}, metadata: {sessionId: 'child'}, time: {start: 1}},
  }]}]);
  translator.snapshot([{info: {id: 'child-message', sessionID: 'child', role: 'assistant'}, parts: [{
    id: 'nested', sessionID: 'child', messageID: 'child-message', type: 'tool', tool: 'm0_fixture_echo', callID: 'child-call',
    state: {status: 'completed', input: {}, output: 'Evidence', time: {start: 2, end: 3}},
  }]}]);
  await writer.drain();
  const start = events.find(event => event.name === 'tool.start' && event.data.call_id === 'child-call');
  const end = events.find(event => event.name === 'tool.end' && event.data.call_id === 'child-call');
  assert.equal(start.data.parent_call_id, 'parent-call');
  assert.equal(end.data.parent_call_id, 'parent-call');
  assert(start.data.aliases.includes('fixture__echo'));
  assert(start.data.aliases.includes('mcp__m0_fixture__echo'));
});

test('authoritative message snapshots do not duplicate normalized replay', async () => {
  const { writer, events } = recorder();
  const translator = new EventTranslator(writer);
  const value = {
    info: { id: 'm', sessionID: 'root', role: 'assistant', time: { created: 1, completed: 2 } },
    parts: [
      {
        id: 'p',
        sessionID: 'root',
        messageID: 'm',
        type: 'text',
        text: 'Final answer',
        time: { start: 1, end: 2 },
      },
    ],
  };
  translator.snapshot([value]);
  translator.snapshot([value]);
  translator.event({
    id: 'live',
    type: 'message.part.updated',
    properties: { sessionID: 'root', part: value.parts[0] },
  });
  await writer.drain();
  assert.equal(events.filter((event) => event.name === 'message.end').length, 1);
  assert.equal(
    events.filter((event) => event.name === 'native' && event.data.name === 'message.snapshot').length,
    1,
  );
});

test('native event queue is bounded', async () => {
  const { writer, stopped } = recorder();
  writer.emit('message.end', { content: 'x'.repeat(9 * 1024 * 1024) });
  assert(stopped());
  await assert.rejects(writer.drain(), { code: 'EVENT_QUEUE_FULL' });
});

test('resource selection, private MCP headers and disabled memory stay explicit', () => {
  const plan = resourcePlan(
    {
      cwd: '/workspace',
      environment: {
        id: 'fixture',
        resources: [
          { id: 'tools', kind: 'tools', config: { names: [] } },
          { id: 'memory', kind: 'memory', config: { mode: 'off' } },
          {
            id: 'knowledge',
            kind: 'mcp',
            config: { transport: 'http', url: 'http://fixture/mcp' },
            credential_refs: { 'headers.Authorization': 'private' },
          },
        ],
      },
    },
    { private: 'Bearer fixture' },
  );
  assert.deepEqual(plan.selection, { '*': false });
  assert.deepEqual(plan.instructions, []);
  assert.equal(plan.mcp.knowledge.enabled, false);
  assert.equal(plan.mcp.knowledge.headers.Authorization, 'Bearer fixture');
  assert.equal(resourcePlan({ cwd: '/workspace' }, {}).selection, undefined);
});

test('unsupported native settings and absent model connections fail preflight', () => {
  const spec = {
    protocol_version: '1.0',
    workspace_id: 'w',
    task_id: 't',
    attempt_id: 'a',
    task: { prompt: 'Fixture' },
    cwd: '/workspace',
    config: {},
  };
  assert.equal(effectiveConfig(spec).contextWindow, 128000);
  assert.throws(() => effectiveConfig({ ...spec, config: { plugin: ['unregistered-code'] } }), {
    code: 'INVALID_CONFIG',
  });
  assert.throws(() => connection(spec, {}), { code: 'MODEL_REQUIRED' });
  assert.throws(
    () =>
      connection(
        {
          ...spec,
          model: { name: 'fake', endpoint: 'https://user:key@example.invalid', credential_ref: 'key' },
        },
        { key: 'fixture' },
      ),
    { code: 'INVALID_MODEL_ENDPOINT' },
  );
});

test('compacted native snapshots do not replace original tool evidence', async () => {
  const { writer, events } = recorder();
  const translator = new EventTranslator(writer);
  const value = {
    info: { id: 'm', sessionID: 'root', role: 'assistant', time: { created: 1 } },
    parts: [
      {
        id: 'p',
        sessionID: 'root',
        messageID: 'm',
        type: 'tool',
        callID: 'c',
        tool: 'read',
        state: {
          status: 'completed',
          input: { path: 'input' },
          output: 'Original output',
          time: { start: 1, end: 2 },
        },
      },
    ],
  };
  translator.snapshot([value]);
  value.parts[0].state.output = 'Old tool result content cleared';
  value.parts[0].state.time.compacted = 3;
  value.info.time.completed = 4;
  translator.snapshot([value]);
  await writer.drain();
  const results = events.filter((event) => event.name === 'tool.end');
  assert.equal(results.length, 1);
  assert.equal(results[0].data.result, 'Original output');
});

test('a pruned-only tool result remains missing', async () => {
  const { writer, events } = recorder();
  const translator = new EventTranslator(writer);
  translator.snapshot([
    {
      info: { id: 'm', sessionID: 'root', role: 'assistant', time: { created: 1, completed: 4 } },
      parts: [
        {
          id: 'p',
          sessionID: 'root',
          messageID: 'm',
          type: 'tool',
          callID: 'c',
          tool: 'read',
          state: {
            status: 'completed',
            input: {},
            output: 'Old tool result content cleared',
            time: { start: 1, end: 2, compacted: 3 },
          },
        },
      ],
    },
  ]);
  await writer.drain();
  assert.equal(events.filter((event) => event.name === 'tool.start').length, 1);
  assert.equal(events.filter((event) => event.name === 'tool.end').length, 0);
});
