import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {createServer} from 'node:http';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {once} from 'node:events';
import test from 'node:test';
import {openModelGateway} from '../dist/index.js';

async function fixture(t, handler) {
  const directory = await mkdtemp(join(tmpdir(), 'eh-model-'));
  const socket = join(directory, 'model.sock');
  const host = createServer(handler);
  host.listen(socket);
  await once(host, 'listening');
  const abort = new AbortController();
  const bridge = await openModelGateway({metadata: {model_gateway: {
    transport: 'unix_http_v1', socket_path: socket, role: 'grader.v1:judge',
  }}}, abort.signal);
  t.after(async () => {
    await bridge.close();
    host.closeAllConnections();
    await new Promise(resolve => host.close(resolve));
    await rm(directory, {recursive: true, force: true});
  });
  return {bridge, abort};
}

test('native HTTP bodies and SSE reach the owned role without network upstream resolution', async t => {
  const observed = [];
  const {bridge} = await fixture(t, async (request, response) => {
    const body = [];
    for await (const chunk of request) body.push(chunk);
    observed.push({path: request.url, headers: request.headers, body: Buffer.concat(body).toString()});
    response.writeHead(200, {'content-type': 'text/event-stream', 'openai-feature': 'native'});
    response.write('data: {"delta":"first"}\n\n');
    setTimeout(() => response.end('data: [DONE]\n\n'), 10);
  });
  const body = JSON.stringify({model: 'pinned', stream: true, messages: [{role: 'user', content: 'Task'}]});
  const result = await fetch(bridge.endpoint + '/chat/completions', {method: 'POST', body,
    headers: {'authorization': 'Bearer role-token', 'openai-beta': 'native'}});
  assert.equal(result.status, 200);
  assert.equal(result.headers.get('openai-feature'), 'native');
  assert.equal(await result.text(), 'data: {"delta":"first"}\n\ndata: [DONE]\n\n');
  assert.equal(observed.length, 1);
  assert.equal(observed[0].path, '/grader.v1:judge/v1/chat/completions');
  assert.equal(observed[0].body, body);
  assert.equal(observed[0].headers.authorization, 'Bearer role-token');
  assert.equal(observed[0].headers['openai-beta'], 'native');
  for (const path of ['/models', '/responses?upstream=elsewhere'])
    assert.equal((await fetch(bridge.endpoint + path, {method: 'POST', body})).status, 404);
  assert.equal(observed.length, 1);
});

test('attempt cancellation tears down an active upstream stream and the local listener', async t => {
  let disconnected;
  const closed = new Promise(resolve => {disconnected = resolve;});
  const {bridge, abort} = await fixture(t, (request, response) => {
    request.resume();
    response.writeHead(200, {'content-type': 'text/event-stream'});
    response.write('data: first\n\n');
    response.on('close', disconnected);
  });
  const result = await fetch(bridge.endpoint + '/responses', {method: 'POST', body: '{}'});
  const reader = result.body.getReader();
  assert.match(new TextDecoder().decode((await reader.read()).value), /first/);
  abort.abort();
  await assert.rejects(reader.read());
  await Promise.race([closed, new Promise((_, reject) => setTimeout(() => reject(new Error('Upstream leaked')), 1000).unref())]);
  await bridge.close();
  await assert.rejects(fetch(bridge.endpoint + '/responses', {method: 'POST', body: '{}'}));
});

test('client disconnect closes its owned upstream connection', async t => {
  let disconnected;
  const closed = new Promise(resolve => {disconnected = resolve;});
  const {bridge} = await fixture(t, (request, response) => {
    request.resume();
    response.writeHead(200, {'content-type': 'text/event-stream'});
    response.write('data: first\n\n');
    response.on('close', disconnected);
  });
  const abort = new AbortController();
  const result = await fetch(bridge.endpoint + '/responses', {method: 'POST', body: '{}', signal: abort.signal});
  await result.body.getReader().read();
  abort.abort();
  await Promise.race([closed, new Promise((_, reject) => setTimeout(() => reject(new Error('Upstream leaked')), 1000).unref())]);
});

test('missing gateway preserves direct operation; cancelled startup never opens a listener', async () => {
  assert.equal(await openModelGateway({}, new AbortController().signal), undefined);
  await assert.rejects(openModelGateway({metadata: {model_gateway: {
    transport: 'unix_http_v1', role: 'agent', socket_path: '/owned/model.sock',
  }}}, AbortSignal.abort()), {code: 'MODEL_GATEWAY_CANCELLED'});
});
