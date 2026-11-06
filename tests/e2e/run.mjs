// Real Work UI -> HTTP API -> runner -> rootless native harness -> local fixture.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {chromium, expect} from '../../frontend/node_modules/@playwright/test/index.mjs';
import {preview} from '../../frontend/node_modules/vite/dist/node/index.js';
import {stream} from '../../harnesses/shared/fake-openai.mjs';

const apiUrl = process.env.E2E_API_URL ?? 'http://api:8000';
const results = process.env.E2E_RESULTS_DIR ?? '/results';
await mkdir(results, {recursive: true});
const observations = [];
const held = new Map();
const counts = new Map();
const fixture = createServer(async (request, response) => {
  try {
    assert.equal(request.url, '/v1/chat/completions');
    assert.equal(request.headers.authorization, 'Bearer local-engineering-key');
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    const text = JSON.stringify(body.messages);
    const match = text.match(/EVALHIVE_CASE:(file|feedback|cancel|failure)-([a-z0-9]+)/);
    assert(match, 'Only engineering fixture prompts may call the endpoint');
    const key = `${match[1]}-${match[2]}`;
    const index = (counts.get(key) ?? 0) + 1;
    counts.set(key, index);
    observations.push({key, index, body});
    if ((match[1] === 'feedback' && index === 1) || (match[1] === 'cancel' && index === 2)) {
      held.set(key, () => stream(response, index, {content: 'Initial engineering response'}));
      response.on('close', () => held.delete(key));
      return;
    }
    const tools = (body.tools ?? []).map(tool => tool.function);
    if (['file', 'cancel', 'failure'].includes(match[1]) && index === 1) {
      const tool = tools.find(tool => tool.name === 'write' || tool.name === 'file_write');
      assert(tool, 'A portable write tool must be available');
      const props = tool.parameters.properties;
      const pathKey = Object.hasOwn(props, 'filePath') ? 'filePath' : 'path';
      stream(response, index, {tool: {id: 'fixture-write', type: 'function', function: {
        name: tool.name, arguments: JSON.stringify({[pathKey]: '/workspace/result.txt', content: 'Engineering artifact\n'}),
      }}});
    } else if (match[1] === 'file' && index === 2) {
      const tool = tools.find(tool => tool.name.endsWith('echo'));
      assert(tool, 'The environment MCP tool must be available');
      stream(response, index, {tool: {id: 'fixture-mcp', type: 'function', function: {
        name: tool.name, arguments: JSON.stringify({text: 'MCP evidence'}),
      }}});
    } else if (match[1] === 'failure') {
      response.writeHead(401, {'Content-Type': 'application/json'});
      response.end(JSON.stringify({error: {message: 'Expected engineering authentication failure'}}));
    } else stream(response, index, {content: 'Engineering run completed. Artifact: result.txt',
      usage: {prompt_tokens: 20, completion_tokens: 3, prompt_tokens_details: {cached_tokens: 0}}});
  } catch (error) {
    observations.push({error: String(error)});
    if (!response.headersSent) response.writeHead(500, {'Content-Type': 'application/json'});
    response.end(JSON.stringify({error: {message: 'Engineering fixture assertion failed'}}));
  }
});
await new Promise(resolve => fixture.listen(4100, '0.0.0.0', resolve));
let headers = {};
async function api(path, method = 'GET', body) {
  let response;
  for (let retry = 0; retry < 3; retry++) {
    try {
      response = await fetch(apiUrl + '/api' + path, {method, headers: {'Content-Type': 'application/json', ...headers},
        ...(body === undefined ? {} : {body: JSON.stringify(body)})});
      break;
    } catch (error) {
      // A closed pooled socket can race a GET. Never replay a mutation here.
      if (method !== 'GET' || retry === 2) throw error;
      await new Promise(resolve => setTimeout(resolve, 100 * (retry + 1)));
    }
  }
  const text = await response.text();
  assert(response.ok, `${method} ${path}: ${response.status} ${text.slice(0, 1000)}`);
  return text ? JSON.parse(text) : null;
}
async function waitFor(read, check, description, timeout = 90000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    last = await read();
    if (check(last)) return last;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`Timed out: ${description}; last=${JSON.stringify(last)}`);
}
let browser, web;
const checks = [];
try {
  await waitFor(async () => {try {return (await fetch(apiUrl + '/api/health')).status;} catch {return 0;}}, status => status === 200, 'API readiness');
  const registration = await api('/auth/register', 'POST', {email: `harness-${Date.now()}@example.com`, password: 'engineering-fixture-password', display_name: 'Engineering fixture'});
  headers = {Authorization: `Bearer ${registration.access_token}`, 'X-Workspace-Id': registration.default_workspace_id};
  const {workspaces} = await api('/auth/me');
  const user = registration.user;
  await api('/settings', 'PATCH', {memory_mode: 'off', max_concurrent_agents: 1});
  const provider = await api('/providers', 'POST', {name: 'Local engineering fixture', endpoint: process.env.E2E_MODEL_URL ?? 'http://browser:4100/v1', api_key: 'local-engineering-key'});
  const model = await api(`/providers/${provider.id}/models`, 'POST', {display_name: 'Local fixture', api_name: 'fake-model'});
  // No orchestrator model is configured: direct mode must work independently.
  const registry = await api('/registry/tools');
  const write = registry.find(tool => tool.kind === 'builtin' && tool.name === 'file_write');
  assert(write, 'The portable builtin registry must be seeded');
  const mcpSource = await readFile(new URL('./mcp-fixture.cjs', import.meta.url), 'utf8');
  const mcp = await api('/registry/tools', 'POST', {name: 'engineering_echo', kind: 'mcp', config: {command: 'node', args: ['-e', mcpSource]}});
  const role = await api('/templates', 'POST', {name: 'Engineering role', description: 'Local fixture resources', soul_md: 'Follow the local engineering task.', execution_mode: 'direct', tool_ids: [write.id, mcp.id], timeout_minutes: 2});
  const profiles = {};
  for (const connector of ['pi', 'opencode']) profiles[connector] = await api('/harness-profiles', 'POST', {
    name: `Engineering ${connector}`, connector_id: connector, model_id: model.id,
    config: connector === 'pi' ? {autoCompaction: false, maxRetries: 0} : {},
  });
  web = await preview({root: '/repo/frontend', configFile: false, preview: {host: '127.0.0.1', port: 3000,
    proxy: {'/api': {target: apiUrl}, '/ws': {target: apiUrl, ws: true}}}});
  browser = await chromium.launch({headless: true});
  const context = await browser.newContext({acceptDownloads: true});
  await context.addInitScript(({registration, workspaces, user}) => localStorage.setItem('evalhive.auth', JSON.stringify({state: {
    token: registration.access_token, user, workspaces, workspaceId: registration.default_workspace_id,
  }, version: 0})), {registration, workspaces, user});
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(String(error)));
  await page.goto('http://127.0.0.1:3000/settings');
  await expect(page.getByRole('heading', {name: 'Harness profiles', exact: true})).toBeVisible();
  await expect(page.getByText('Engineering pi', {exact: true})).toBeVisible();
  await expect(page.getByText('Engineering opencode', {exact: true})).toBeVisible();
  checks.push('Settings renders installed connector profiles');
  for (const connector of ['pi', 'opencode']) {
    for (const kind of ['file', 'feedback', 'cancel', 'failure']) {
      const key = `${kind}-${connector}`;
      const title = `EVALHIVE_CASE:${key}`;
      await page.goto('http://127.0.0.1:3000/tasks');
      await page.getByRole('button', {name: 'New Task', exact: true}).click();
      await page.getByPlaceholder('Task title...').fill(title);
      await page.getByPlaceholder('Describe the task...').fill('Run the local engineering fixture and produce the requested evidence.');
      await expect(page.getByLabel('Task execution mode')).toHaveValue('direct');
      await page.getByLabel('Task harness profile').selectOption(profiles[connector].id);
      if (kind !== 'feedback') await page.getByLabel('Task role', {exact: true}).selectOption(role.id);
      const created = page.waitForResponse(response => response.url().endsWith('/api/tasks') && response.request().method() === 'POST');
      await page.getByRole('button', {name: 'Create Task', exact: true}).click();
      const task = await (await created).json();
      await page.getByText(title, {exact: true}).click();
      await page.getByRole('button', {name: 'Start Task', exact: true}).click();
      if (kind === 'feedback' || kind === 'cancel') {
        await waitFor(async () => held.has(key), Boolean, `held ${key}`);
        if (kind === 'feedback') {
          await page.getByLabel('Feedback for the running attempt').fill('Apply the additional engineering instruction.');
          await page.getByRole('button', {name: 'Send feedback', exact: true}).click();
          await waitFor(() => api(`/tasks/${task.id}/attempts`).then(async attempts => attempts.length ? api(`/attempts/${attempts[0].id}/commands`) : []),
            commands => commands.some(command => command.name === 'feedback' && command.status === 'acknowledged'), 'feedback acknowledgement');
          held.get(key)?.();
        } else await page.getByRole('button', {name: 'Cancel execution', exact: true}).click();
      }
      const finished = await waitFor(() => api(`/tasks/${task.id}`), task => ['done', 'failed'].includes(task.status), `${key} terminal task`);
      const attempts = await api(`/tasks/${task.id}/attempts`);
      assert.equal(attempts.length, 1);
      const attempt = await api(`/attempts/${attempts[0].id}`);
      assert.equal(attempt.status, kind === 'cancel' ? 'cancelled' : kind === 'failure' ? 'failed' : 'success', JSON.stringify(attempt.result));
      assert.equal(finished.status, ['cancel', 'failure'].includes(kind) ? 'failed' : 'done');
      assert.equal(finished.orchestrator_cost_usd, 0);
      assert.deepEqual(finished.orchestrator_usage, {});
      assert.equal(finished.cost_usd, null);
      const events = [];
      let cursor = -1;
      for (;;) {
        const batch = await api(`/attempts/${attempt.id}/events?after_seq=${cursor}&limit=500`);
        if (!batch.length) break;
        events.push(...batch); cursor = batch.at(-1).seq;
      }
      assert.deepEqual(events.map(event => event.seq), events.map((_, index) => index));
      assert.equal(events.at(-1).event, 'run.finished');
      assert(!JSON.stringify(events).includes('local-engineering-key'), 'Private credentials must not enter the journal');
      assert(attempt.prepared_at,
        'The applied configuration must be frozen before execution');
      if (kind !== 'feedback') {
        assert.equal(finished.result_files.length, 1);
        assert(events.some(event => event.event === 'tool.end' && event.data.call_id === 'fixture-write'));
        if (kind === 'file') assert(events.some(event => event.event === 'tool.end' && event.data.call_id === 'fixture-mcp'));
        await expect(page.getByRole('button', {name: 'result.txt', exact: true})).toBeVisible({timeout: 15000});
        const download = page.waitForEvent('download');
        await page.getByRole('button', {name: 'result.txt', exact: true}).click();
        const file = await download;
        await file.saveAs(`${results}/${connector}-result.txt`);
        assert.equal(await readFile(`${results}/${connector}-result.txt`, 'utf8'), 'Engineering artifact\n');
        assert.equal(await file.failure(), null);
      }
      if (kind === 'feedback') {
        assert(observations.filter(record => record.key === key).some(record => JSON.stringify(record.body).includes('additional engineering instruction')));
      }
      await page.screenshot({path: `${results}/${key}.png`, fullPage: true});
      checks.push(`${connector}: ${kind} through Work, native SDK and durable journal`);
      console.log(`PASS ${connector} ${kind}`);
    }
  }
  assert.deepEqual(pageErrors, []);
  assert(!observations.some(record => record.error), JSON.stringify(observations.filter(record => record.error)));
  await writeFile(`${results}/acceptance.json`, JSON.stringify({status: 'passed', checks, model_calls: observations.length, scientific_experiments: 0}, null, 2));
} catch (error) {
  await writeFile(`${results}/failure.json`, JSON.stringify({error: String(error), stack: error.stack, checks, observations}, null, 2));
  throw error;
} finally {
  await browser?.close();
  await new Promise(resolve => web?.httpServer.close(resolve) ?? resolve());
  fixture.closeAllConnections();
  await new Promise(resolve => fixture.close(resolve));
}
