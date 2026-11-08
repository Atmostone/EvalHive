import assert from 'node:assert/strict';
import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fakeOpenAI} from './fake-openai.mjs';
import {fakeMcpHTTP} from './fake-mcp-http.mjs';

export async function conversationFixture(entrypoint, handler, {mode = 'conversation', history = [], resources = []} = {}) {
  const {ConnectorClient} = await import(new URL('../../sdk/typescript/dist/index.js', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'evalhive-conversation-'));
  const cwd = join(directory, 'workspace'); mkdirSync(cwd);
  const privateFile = join(directory, 'private.json');
  writeFileSync(privateFile, JSON.stringify({model_api_key: 'local-fixture-key'}), {mode: 0o600});
  const failures = [];
  const endpoint = await fakeOpenAI(async (...args) => {try {await handler(...args);} catch (error) {failures.push(error); throw error;}});
  const client = ConnectorClient.launch(['node', entrypoint], {env: {PATH: process.env.PATH,
    HOME: directory, TMPDIR: tmpdir(), EVALHIVE_CREDENTIALS_FILE: privateFile}, eventBuffer: 512});
  const events = [];
  const collect = (async () => {for await (const event of client.events()) events.push(event);})();
  collect.catch(error => failures.push(error));
  const spec = {protocol_version: '1.0', workspace_id: 'workspace', task_id: 'task', attempt_id: 'attempt', cwd,
    task: {prompt: 'This placeholder must not generate a model request.'}, config: {autoCompaction: false},
    environment: {id: 'fixture', resources}, limits: {max_turns: 8},
    interaction: {version: '1.0', mode, initial_history: history},
    model: {name: 'fake-model', endpoint: endpoint.url, credential_ref: 'model_api_key'}};
  const wait = async (kind, predicate = () => true) => {
    const deadline = Date.now() + 40000;
    for (;;) {
      if (failures.length) throw failures[0];
      const match = events.find(event => event.event === kind && predicate(event));
      if (match) return match;
      if (events.some(event => event.event === 'run.finished')) throw Error('Premature completion: ' + JSON.stringify(events.at(-1)));
      if (Date.now() > deadline) throw Error('Timed out waiting for ' + kind);
      await new Promise(resolve => setTimeout(resolve, 20));
    }
  };
  const control = (command, payload = {}) => client.request('control', {attempt_id: 'attempt', command, payload});
  return {client, endpoint, spec, events, wait, control,
    submit: (index, text) => control('turn.submit', {input_id: 'input-' + index, turn_id: 'turn-' + index,
      expected_revision: index - 1, messages: [{role: 'user', content: text}]}),
    async start() {await client.request('validate', {run: spec}); await client.request('start', {run: spec}); await wait('session.waiting_input');},
    async close() {await client.close(1000); await collect.catch(() => {}); await endpoint.close(); rmSync(directory, {recursive: true, force: true});},
  };
}

export async function exerciseConversation(entrypoint, stream) {
  const fixture = await conversationFixture(entrypoint, (body, index, response) => {
    assert(body.messages.some(item => item.role === 'assistant' && JSON.stringify(item.content).includes('Seeded answer')));
    assert(body.messages.some(item => item.role === 'user' && JSON.stringify(item.content).includes('First user')));
    assert(body.messages.some(item => item.role === 'tool' && item.tool_call_id === 'seed-call' && JSON.stringify(item.content).includes('Seed tool result')));
    if (index === 2) assert(body.messages.some(item => item.role === 'assistant' && JSON.stringify(item.content).includes('Reply 1')));
    stream(response, index, {content: 'Reply ' + index});
  }, {history: [{role: 'user', content: 'Seeded question'}, {role: 'assistant', content: 'Seeded answer',
    tool_calls: [{call_id: 'seed-call', name: 'read', arguments: {path: '/old-fixture.txt'}}]},
    {role: 'tool', call_id: 'seed-call', content: 'Seed tool result'}, {role: 'assistant', content: 'Seeded conclusion'}]});
  try {
    await fixture.start();
    const receipt = await fixture.submit(1, 'First user');
    await fixture.wait('session.waiting_input', event => event.data.revision === 1);
    assert(!fixture.events.some(event => event.event === 'run.finished'));
    assert.deepEqual(await fixture.submit(1, 'First user'), receipt);
    await assert.rejects(fixture.submit(1, 'Conflicting user'), error => error.code === 'INPUT_CONFLICT');
    await fixture.submit(2, 'Second user');
    await fixture.wait('session.waiting_input', event => event.data.revision === 2);
    await fixture.control('session.close', {input_id: 'close', expected_revision: 2, reason: 'completed'});
    const terminal = await fixture.wait('run.finished');
    assert.equal(terminal.data.status, 'success', JSON.stringify(terminal.data));
    assert.equal(fixture.endpoint.requests.length, 2);
    assert.equal(fixture.events.filter(event => event.event === 'turn.completed').length, 2);
    assert(!fixture.events.some(event => event.event.startsWith('tool.') && event.data.call_id === 'seed-call'));
    assert.equal(new Set(fixture.events.filter(event => event.event === 'usage').map(event => event.data.usage_id)).size, 2);
  } finally {await fixture.close();}
}

export async function exerciseCancellation(entrypoint, {generating = false} = {}) {
  const fixture = await conversationFixture(entrypoint, (_body, _index, response) => {
    response.writeHead(200, {'Content-Type': 'text/event-stream'});
    response.write('data: {"id":"pending","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Working"},"finish_reason":null}]}\n\n');
  });
  try {
    await fixture.start();
    if (generating) {await fixture.submit(1, 'Generate'); await fixture.endpoint.waitForRequest();}
    await fixture.control('abort');
    assert.equal((await fixture.wait('run.finished')).data.status, 'cancelled');
    assert.equal(fixture.endpoint.requests.length, generating ? 1 : 0);
  } finally {await fixture.close();}
}

export async function exerciseNativeFailure(entrypoint) {
  const fixture = await conversationFixture(entrypoint, (_body, _index, response) => {
    response.writeHead(401, {'Content-Type': 'application/json'}); response.end(JSON.stringify({error: {message: 'Local fixture error'}}));
  });
  try {
    await fixture.start(); await fixture.submit(1, 'Generate');
    assert.equal((await fixture.wait('run.finished')).data.status, 'failed');
    assert(!fixture.events.some(event => event.event === 'turn.completed'));
  } finally {await fixture.close();}
}

export async function exercisePreflight(entrypoint) {
  const fixture = await conversationFixture(entrypoint, () => {throw Error('Preflight must not request a model');}, {mode: 'external_steps',
    resources: [{id: 'tools', kind: 'tools', config: {names: ['bash']}}]});
  try {
    await assert.rejects(fixture.client.request('validate', {run: fixture.spec}), error => error.code === 'UNSUPPORTED_TOOL');
    fixture.spec.environment.resources = [];
    fixture.spec.interaction.initial_history = [{role: 'tool', call_id: 'orphan', content: 'invalid'}];
    await assert.rejects(fixture.client.request('validate', {run: fixture.spec}), error => error.code === 'INVALID_HISTORY');
    assert.equal(fixture.endpoint.requests.length, 0);
  } finally {await fixture.close();}
}

export async function exerciseExternalTools(entrypoint, stream, {cancel = false, error = false} = {}) {
  const mcp = await fakeMcpHTTP(() => {}, [{name: 'lookup', description: 'Lookup fixture state',
    inputSchema: {type: 'object', properties: {query: {type: 'string'}}, required: ['query'], additionalProperties: false}}]);
  const fixture = await conversationFixture(entrypoint, (body, index, response) => {
    assert(!body.tools.some(tool => ['bash', 'write', 'read', 'task'].includes(tool.function.name)));
    if (index === 1) {
      const name = body.tools.find(tool => tool.function.name.endsWith('lookup'))?.function.name;
      assert(name, JSON.stringify(body.tools));
      stream(response, index, {tools: ['call-a', 'call-b'].map(id => ({id, type: 'function', function: {name,
        arguments: JSON.stringify({query: 'same arguments'})}}))});
    } else {
      const results = body.messages.filter(message => message.role === 'tool');
      assert.deepEqual(new Set(results.map(item => item.tool_call_id)), new Set(['call-a', 'call-b']));
      for (const [id, expected] of [['call-a', 'Result A'], ['call-b', 'Result B']])
        assert(JSON.stringify(results.find(item => item.tool_call_id === id).content).includes(expected));
      assert(!JSON.stringify(body.messages).includes('__evalhive_native_call_id'));
      stream(response, index, {content: 'Tools completed'});
    }
  }, {mode: 'external_steps', resources: [{id: 'domain', kind: 'mcp', config: {transport: 'http', url: mcp.url}}]});
  try {
    await fixture.start(); await fixture.submit(1, 'Use the tools.');
    const request = await fixture.wait('tool.request');
    assert.deepEqual(request.data.calls.map(call => call.call_id), ['call-a', 'call-b']);
    assert.equal(fixture.endpoint.requests.length, 1);
    const respond = (id, content, inputId) => fixture.control('tool.respond', {input_id: inputId,
      turn_id: 'turn-1', batch_id: request.data.batch_id, expected_revision: 1,
      results: [{call_id: id, content, is_error: error && id === 'call-b'}]});
    if (cancel) {
      await fixture.control('abort');
      assert.equal((await fixture.wait('run.finished')).data.status, 'cancelled');
      assert.equal(fixture.endpoint.requests.length, 1);
    } else {
      const receipt = await respond('call-b', 'Result B', 'response-b');
      assert.deepEqual(await respond('call-b', 'Result B', 'response-b'), receipt);
      await respond('call-a', 'Result A', 'response-a');
      await fixture.wait('session.waiting_input', event => event.data.revision === 1);
      await fixture.control('session.close', {input_id: 'close', expected_revision: 1, reason: 'completed'});
      const terminal = await fixture.wait('run.finished');
      assert.equal(terminal.data.status, 'success', JSON.stringify(terminal.data));
      assert.equal(fixture.endpoint.requests.length, 2);
      assert.equal(fixture.events.find(event => event.event === 'tool.end' && event.data.call_id === 'call-b')?.data.status,
        error ? 'error' : 'completed');
      const map = fixture.events.find(event => event.event === 'native' && event.data.name === 'external_tools.mapping');
      assert(map?.data.payload.tools.some(tool => tool.canonical_name === 'mcp__domain__lookup' && /^[a-f0-9]{64}$/.test(tool.schema_sha256)));
    }
    assert(!mcp.requests.some(item => item.method === 'tools/call'), 'The connector must not execute the benchmark tool');
  } finally {await fixture.close(); await mcp.close();}
}

export async function exerciseExternalRounds(entrypoint, stream) {
  const mcp = await fakeMcpHTTP();
  const fixture = await conversationFixture(entrypoint, (body, index, response) => {
    if (index <= 2) stream(response, index, {tool: {id: 'round-' + index, type: 'function', function: {
      name: body.tools.find(tool => tool.function.name.endsWith('search_knowledge_base')).function.name,
      arguments: JSON.stringify({query: 'round ' + index}),
    }}});
    else stream(response, index, {content: 'Two batches completed'});
  }, {mode: 'external_steps', resources: [{id: 'domain', kind: 'mcp', config: {transport: 'http', url: mcp.url}}]});
  try {
    await fixture.start(); await fixture.submit(1, 'Use two rounds of tools');
    for (const index of [1, 2]) {
      const request = await fixture.wait('tool.request', event => event.data.calls[0].call_id === 'round-' + index);
      await fixture.control('tool.respond', {input_id: 'result-' + index, turn_id: 'turn-1',
        batch_id: request.data.batch_id, expected_revision: 1,
        results: [{call_id: 'round-' + index, content: 'Round result', is_error: false}]});
    }
    await fixture.wait('session.waiting_input', event => event.data.revision === 1);
    await fixture.control('session.close', {input_id: 'close', expected_revision: 1, reason: 'complete'});
    assert.equal((await fixture.wait('run.finished')).data.status, 'success');
    assert.equal(fixture.endpoint.requests.length, 3);
    assert.equal(fixture.events.filter(event => event.event === 'turn.completed').length, 1);
    assert(!mcp.requests.some(item => item.method === 'tools/call'));
  } finally {await fixture.close(); await mcp.close();}
}
