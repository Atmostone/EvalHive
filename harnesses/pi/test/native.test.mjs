import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { ConnectorClient } from '@evalhive/harness-sdk';
import { fakeOpenAI, stream } from '../../shared/fake-openai.mjs';
import { fakeMcpHTTP } from '../../shared/fake-mcp-http.mjs';

const entrypoint = fileURLToPath(new URL('../dist/index.js', import.meta.url));
async function runFixture(handler, {control, config = {}, limits = {}, resources = [], prepare, privateCredentials = {}} = {}) {
  const endpoint = await fakeOpenAI(handler);
  const directory = mkdtempSync(join(tmpdir(), 'evalhive-pi-test-'));
  const workspace = join(directory, 'workspace');
  mkdirSync(workspace);
  if (prepare) resources = await prepare(workspace);
  const credentials = join(directory, 'private.json');
  writeFileSync(credentials, JSON.stringify({model_api_key: 'fake-key-for-local-tests', ...privateCredentials}), {mode: 0o600});
  const client = ConnectorClient.launch(['node', entrypoint], {env: {PATH: process.env.PATH,
    HOME: directory, TMPDIR: tmpdir(), EVALHIVE_CREDENTIALS_FILE: credentials}});
  const spec = {protocol_version: '1.0', workspace_id: 'workspace', task_id: 'task', attempt_id: 'attempt',
    task: {prompt: 'Create the requested result and finish.'}, cwd: workspace, config,
    instructions: ['This is a local engineering fixture.'], limits,
    environment: {id: 'fixture', resources},
    model: {name: 'fake-model', endpoint: endpoint.url, credential_ref: 'model_api_key'}};
  const events = [];
  const collect = (async () => {
    for await (const event of client.events()) {
      events.push(event);
      if (event.event === 'run.finished') return event.data;
    }
    throw new Error('Pi exited without a terminal record');
  })();
  // Observe the collector even if preflight fails before the assertion below.
  collect.catch(() => {});
  try {
    assert.equal((await client.request('describe')).requirements.harness_version, '1.5.0');
    assert.equal((await client.request('validate', {run: spec})).accepted, true);
    await client.request('start', {run: spec});
    if (control) await control(client, endpoint, spec, events);
    const result = await collect;
    assert.deepEqual(events.map((event) => event.seq), events.map((_, index) => index));
    return {result, events, requests: endpoint.requests,
      output: existsSync(join(workspace, 'result.txt')) ? readFileSync(join(workspace, 'result.txt'), 'utf8') : null};
  } finally {
    // Read assertions supplied by handlers can inspect output before this cleanup.
    await client.close(1000);
    await endpoint.close();
    rmSync(directory, {recursive: true, force: true});
  }
}

test('official Pi SDK completes with normalized provider usage', {timeout: 20000}, async () => {
  const {result, events, requests} = await runFixture((body, index, res, req) => {
    assert.equal(req.headers.authorization, 'Bearer fake-key-for-local-tests');
    assert.equal(req.headers['user-agent'], 'evalhive/0.1');
    assert.equal(body.temperature, 0.25);
    assert.equal(body.seed, 7);
    stream(res, index, {content: 'Completed'});
  }, {config: {temperature: 0.25, seed: 7}});
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(requests.length, 1);
  assert(events.some((event) => event.event === 'native' && event.data.name === 'agent_settled'));
  const usage = events.filter((event) => event.event === 'usage').at(-1).data;
  assert.equal(usage.input_tokens, 10);
  assert.equal(usage.output_tokens, 2);
  assert.equal(usage.cache_read_tokens, 2);
  assert.equal(usage.reasoning_tokens, 1);
  assert.equal(usage.cost, null);
});

test('official Pi executes its native write tool', {timeout: 20000}, async () => {
  let observedFile;
  const {result, events, requests, output} = await runFixture((body, index, res) => {
    if (index === 1) stream(res, index, {tool: {id: 'write-1', type: 'function', function: {
      name: 'write', arguments: JSON.stringify({path: 'result.txt', content: 'Engineering result\n'}),
    }}});
    else {
      const response = body.messages.find((message) => message.role === 'tool');
      assert.equal(response.tool_call_id, 'write-1');
      observedFile = body.messages.some((message) => message.role === 'assistant');
      stream(res, index, {content: 'File created'});
    }
  });
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(requests.length, 2);
  assert.equal(output, 'Engineering result\n');
  assert(observedFile);
  const calls = events.filter((event) => event.event.startsWith('tool.'));
  assert.deepEqual(calls.map((event) => event.event), ['tool.start', 'tool.end']);
  assert.equal(calls[0].data.call_id, calls[1].data.call_id);
  assert.equal(calls[0].data.name, 'write');
});

test('absent provider usage remains unknown despite Pi numeric defaults', {timeout: 20000}, async () => {
  const {result, events} = await runFixture((body, index, res) => stream(res, index, {content: 'No usage', usage: false}));
  assert.equal(result.status, 'success', JSON.stringify(result));
  const usage = events.filter((event) => event.event === 'usage').at(-1).data;
  assert.equal(usage.input_tokens, null);
  assert.equal(usage.output_tokens, null);
  assert.equal(usage.cache_read_tokens, null);
  assert.equal(usage.cost, null);
});

test('Pi automatic retry completes before the SDK terminal event', {timeout: 20000}, async () => {
  const {result, events, requests} = await runFixture((body, index, res) => {
    if (index === 1) { res.writeHead(500, {'Content-Type': 'application/json'}); res.end(JSON.stringify({error: {message: 'Temporary fixture failure'}})); }
    else stream(res, index, {content: 'Recovered'});
  }, {config: {maxRetries: 1, retryBaseDelayMs: 1}});
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(requests.length, 2);
  assert(events.some((event) => event.event === 'native' && event.data.name === 'auto_retry_start'));
  assert.equal(events.filter(event => event.event === 'failure.observed').length, 1);
  assert.equal(events.find(event => event.event === 'failure.observed').data.facts.status_code, 500);
  assert.equal(events.at(-1).event, 'run.finished');
});

test('native compaction requests retain independent raw usage availability', {timeout: 30000}, async () => {
  const {result, events, requests} = await runFixture((body, index, res) => {
    if (index === 1) stream(res, index, {tool: {id: 'write-1', type: 'function', function: {
      name: 'write', arguments: JSON.stringify({path: 'result.txt', content: 'x'.repeat(1200)}),
    }}, usage: {prompt_tokens: 3900, completion_tokens: 20}});
    else stream(res, index, {content: 'Completed summary or final answer',
      usage: {prompt_tokens: 15, completion_tokens: 5, completion_tokens_details: {reasoning_tokens: 0}}});
  }, {config: {contextWindow: 4096, maxOutputTokens: 64, compaction: {reserveTokens: 512, keepRecentTokens: 32}}});
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert(events.some(event => event.event === 'native' && event.data.name === 'compaction_end'));
  const usage = new Map(events.filter(event => event.event === 'usage').map(event => [event.data.usage_id, event.data]));
  assert.equal(usage.size, requests.length);
  assert([...usage.values()].some(value => value.purpose === 'compaction'));
  for (const value of usage.values()) {
    assert.equal(value.cache_read_tokens, null);
    assert.equal(value.cache_write_tokens, null);
    assert.equal(value.cost, null);
    assert.equal(value.reasoning_tokens, value.usage_id === 'request-1' ? null : 0);
  }
});

test('Pi HTTP failure does not become a successful process exit', {timeout: 20000}, async () => {
  const {result} = await runFixture((body, index, res) => {
    res.writeHead(401, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({error: {message: 'Expected local authentication failure'}}));
  });
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'PI_INCOMPLETE');
  assert.deepEqual(result.error.details.normalized_failure, {kind: 'model_error', status_code: 401, exception: null});
});

test('Pi cancellation interrupts a live provider stream', {timeout: 20000}, async () => {
  const {result} = await runFixture((body, index, res) => {
    res.writeHead(200, {'Content-Type': 'text/event-stream'});
    res.write(`data: ${JSON.stringify({id: 'hang', object: 'chat.completion.chunk', created: 1, model: 'fake-model', choices: [{index: 0, delta: {role: 'assistant', content: 'Working'}, finish_reason: null}]})}\n\n`);
  }, {control: async (client, endpoint, spec) => {
    await endpoint.waitForRequest();
    await client.request('control', {attempt_id: spec.attempt_id, command: 'abort'});
  }});
  assert.equal(result.status, 'cancelled');
});

async function waitFor(check) {
  const deadline = Date.now() + 5000;
  while (!check()) {
    if (Date.now() >= deadline) throw new Error('Expected native event did not arrive');
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

test('feedback reaches Pi before it settles', {timeout: 20000}, async () => {
  let release;
  let feedbackInRequest = false;
  const {result, requests} = await runFixture(async (body, index, res) => {
    if (index === 1) {
      await new Promise((resolve) => { release = resolve; });
      stream(res, index, {content: 'First answer'});
    } else {
      feedbackInRequest = body.messages.some((message) => message.role === 'user' && JSON.stringify(message.content).includes('Additional instruction'));
      stream(res, index, {content: 'Feedback applied'});
    }
  }, {control: async (client, endpoint, spec) => {
    await endpoint.waitForRequest();
    const receipt = await client.request('control', {attempt_id: spec.attempt_id, command: 'feedback', payload: {text: 'Additional instruction'}});
    assert.equal(receipt.accepted, true);
    release();
  }});
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(requests.length, 2);
  assert(feedbackInRequest);
});

test('turn limit stops Pi before another model request', {timeout: 20000}, async () => {
  const {result, requests, output} = await runFixture((body, index, res) => stream(res, index, {tool: {
    id: `write-${index}`, type: 'function', function: {name: 'write', arguments: JSON.stringify({path: 'result.txt', content: 'Retained at limit'})},
  }}), {limits: {max_turns: 1}});
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'TURN_LIMIT');
  assert.equal(requests.length, 1);
  assert.equal(output, 'Retained at limit');
});

test('Pi cancellation interrupts an active bash tool', {timeout: 20000}, async () => {
  const {result, events} = await runFixture((body, index, res) => stream(res, index, {tool: {
    id: 'bash-wait', type: 'function', function: {name: 'bash', arguments: JSON.stringify({command: 'sleep 30'})},
  }}), {control: async (client, endpoint, spec, events) => {
    await waitFor(() => events.some((event) => event.event === 'tool.start'));
    await client.request('control', {attempt_id: spec.attempt_id, command: 'abort'});
  }});
  assert.equal(result.status, 'cancelled');
  assert(events.some((event) => event.event === 'tool.end'));
});

test('explicit empty toolset and memory off override native discovery', {timeout: 20000}, async () => {
  const {result} = await runFixture((body, index, res) => {
    assert.equal(body.tools?.length ?? 0, 0);
    assert(!JSON.stringify(body.messages).includes('FORBIDDEN_DISCOVERED_MEMORY'));
    stream(res, index, {content: 'No tools or memory'});
  }, {prepare(workspace) {
    writeFileSync(join(workspace, 'AGENTS.md'), 'FORBIDDEN_DISCOVERED_MEMORY');
    return [{id: 'toolset', kind: 'tools', config: {names: []}},
      {id: 'memory', kind: 'memory', config: {mode: 'off'}}];
  }});
  assert.equal(result.status, 'success', JSON.stringify(result));
});

test('explicit skills, memory and native builtin aliases reach Pi', {timeout: 20000}, async () => {
  const {result} = await runFixture((body, index, res) => {
    const context = JSON.stringify(body.messages);
    assert(context.includes('REMEMBER_FIXTURE_RULE'));
    assert(context.includes('fixture-skill'));
    assert.deepEqual(body.tools.map((tool) => tool.function.name), ['read']);
    stream(res, index, {content: 'Configured resources'});
  }, {prepare(workspace) {
    const skill = join(workspace, 'SKILL.md');
    writeFileSync(skill, '---\nname: fixture-skill\ndescription: Read the engineering fixture.\n---\nUse fixture data.\n');
    return [{id: 'toolset', kind: 'tools', config: {names: ['file_read']}},
      {id: 'memory', kind: 'memory', config: {mode: 'structured', content: 'REMEMBER_FIXTURE_RULE'}},
      {id: 'skill', kind: 'skill', config: {path: skill}}];
  }});
  assert.equal(result.status, 'success', JSON.stringify(result));
});

test('official MCP client exposes calls with native names and canonical aliases', {timeout: 20000}, async () => {
  const {result, events} = await runFixture((body, index, res) => {
    assert.deepEqual(body.tools.map((tool) => tool.function.name), ['mcp__fixture__echo']);
    if (index === 1) stream(res, index, {tool: {id: 'mcp-1', type: 'function', function: {
      name: 'mcp__fixture__echo', arguments: JSON.stringify({text: 'fixture-value'})}}});
    else {
      assert(JSON.stringify(body.messages).includes('fixture-value'));
      stream(res, index, {content: 'MCP completed'});
    }
  }, {resources: [{id: 'toolset', kind: 'tools', config: {names: ['search_knowledge_base']}},
    {id: 'fixture', kind: 'mcp', config: {transport: 'stdio', command: process.execPath,
      args: [fileURLToPath(new URL('../../shared/fake-mcp.mjs', import.meta.url))], aliases: {echo: 'search_knowledge_base'}}}]});
  assert.equal(result.status, 'success', JSON.stringify(result));
  const call = events.find((event) => event.event === 'tool.start');
  assert.equal(call.data.name, 'mcp__fixture__echo');
  assert.deepEqual(call.data.aliases, ['search_knowledge_base', 'mcp__fixture__echo']);
  assert.equal(events.find((event) => event.event === 'tool.end').data.status, 'completed');
});

test('unavailable required MCP fails before any model request', {timeout: 20000}, async () => {
  const {result, requests} = await runFixture(() => { throw new Error('Model must not run'); },
    {resources: [{id: 'missing', kind: 'mcp', config: {transport: 'stdio', command: '/missing/fixture-mcp'}}]});
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'MCP_UNAVAILABLE');
  assert.equal(requests.length, 0);
});

test('HTTP MCP resolves private authorization and knowledge aliases', {timeout: 20000}, async () => {
  const mcp = await fakeMcpHTTP((req) => assert.equal(req.headers.authorization, 'Bearer fixture-knowledge-token'));
  try {
    const {result, events} = await runFixture((body, index, res) => {
      if (index === 1) stream(res, index, {tool: {id: 'knowledge-1', type: 'function', function: {
        name: 'mcp__knowledge__search_knowledge_base', arguments: JSON.stringify({query: 'fixture'})}}});
      else {
        assert(JSON.stringify(body.messages).includes('Knowledge fixture result'));
        stream(res, index, {content: 'Knowledge read'});
      }
    }, {privateCredentials: {knowledge: 'Bearer fixture-knowledge-token'}, resources: [
      {id: 'tools', kind: 'tools', config: {names: ['search_knowledge_base']}},
      {id: 'knowledge', kind: 'mcp', config: {transport: 'http', url: mcp.url,
        aliases: {search_knowledge_base: 'search_knowledge_base'}}, credential_refs: {'headers.Authorization': 'knowledge'}}]});
    assert.equal(result.status, 'success', JSON.stringify(result));
    assert(!JSON.stringify(events).includes('fixture-knowledge-token'));
    assert(mcp.requests.some((request) => request.method === 'tools/call'));
  } finally { await mcp.close(); }
});
