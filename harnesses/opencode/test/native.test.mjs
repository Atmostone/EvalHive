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

test('native retry survives a recorded upstream connection failure', {timeout: 45000}, async () => {
  const {result, events, requests} = await runFixture((body, index, res) => {
    if (index === 1) res.destroy();
    else stream(res, index, {content: 'Recovered after the local transport failure'});
  });
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(requests.length, 2);
  const failures = events.filter(event => event.event === 'failure.observed');
  assert.equal(failures.length, 1);
  assert.equal(failures[0].data.facts.exception, 'APIConnectionError');
});

test('native nested sessions correlate tools to their parent task call', {timeout: 60000}, async () => {
  const {result, events, requests} = await runFixture((body, index, res) => {
    if (index === 1) stream(res, index, {tool: {id: 'parent-task', type: 'function', function: {
      name: 'task', arguments: JSON.stringify({description: 'Read local fixture',
        prompt: 'Read /workspace/input.txt and report its content.', subagent_type: 'general'}),
    }}});
    else if (index === 2) stream(res, index, {tool: {id: 'child-read', type: 'function', function: {
      name: 'read', arguments: JSON.stringify({filePath: '/workspace/input.txt'}),
    }}});
    else stream(res, index, {content: index === 3 ? 'Child read the local fixture.' : 'Nested task completed.'});
  }, {prepare: workspace => {
    writeFileSync(join(workspace, 'input.txt'), 'Local nested evidence');
    return [{id: 'tools', kind: 'tools', config: {names: ['task', 'read']}}];
  }});
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(requests.length, 4);
  const child = events.find(event => event.event === 'tool.start' && event.data.call_id === 'child-read');
  assert(child?.parent_session_id);
  assert.equal(child.data.parent_call_id, 'parent-task');
  assert.equal(events.find(event => event.event === 'tool.end' && event.data.call_id === 'child-read').data.parent_call_id, 'parent-task');
});

async function runFixture(
  handler,
  { config = {}, resources = [], control, prepare, privateCredentials = {}, bootstrap, limits = {} } = {},
) {
  const directory = mkdtempSync(join(tmpdir(), 'evalhive-opencode-test-'));
  const workspace = join(directory, 'workspace');
  mkdirSync(workspace);
  if (prepare) resources = await prepare(workspace);
  let rejectHandler;
  const handlerFailure = new Promise((_, reject) => {
    rejectHandler = reject;
  });
  handlerFailure.catch(() => {});
  const endpoint = await fakeOpenAI(async (body, index, res, req) => {
    try {
      await handler(body, index, res, req, workspace);
    } catch (error) {
      rejectHandler(error);
      throw error;
    }
  });
  const credentials = join(directory, 'private.json');
  writeFileSync(
    credentials,
    JSON.stringify({ model_api_key: 'fake-key-for-local-tests', ...privateCredentials }),
    { mode: 0o600 },
  );
  const client = ConnectorClient.launch(
    bootstrap ? ['node', '--input-type=module', '-e', bootstrap] : ['node', entrypoint],
    {
      env: {
        PATH: process.env.PATH,
        HOME: directory,
        TMPDIR: tmpdir(),
        EVALHIVE_CREDENTIALS_FILE: credentials,
      },
      eventBuffer: 512,
    },
  );
  const spec = {
    protocol_version: '1.0',
    workspace_id: 'workspace',
    task_id: 'task',
    attempt_id: 'attempt',
    task: { title: 'Engineering fixture', prompt: 'Create the requested result and finish.' },
    cwd: workspace,
    config: { autoCompaction: false, ...config },
    environment: { id: 'fixture', resources },
    limits,
    instructions: ['This is a local engineering fixture.'],
    model: { name: 'fake-model', endpoint: endpoint.url, credential_ref: 'model_api_key' },
  };
  const events = [];
  const collect = (async () => {
    for await (const event of client.events()) {
      events.push(event);
      if (event.event === 'run.finished') return event.data;
    }
    throw new Error('OpenCode exited without a terminal record');
  })();
  collect.catch(() => {});
  let timeout;
  try {
    assert.equal((await client.request('describe')).requirements.harness_version, '1.19.5');
    assert.equal((await client.request('validate', { run: spec }, 20000)).accepted, true);
    await client.request('start', { run: spec }, 20000);
    const result = await Promise.race([
      (async () => {
        if (control) await control(client, endpoint, spec, events);
        return await collect;
      })(),
      handlerFailure,
      new Promise((_, reject) => {
        timeout = setTimeout(
          () =>
            reject(
              new Error(
                'OpenCode fixture did not finish: ' +
                  JSON.stringify({
                    requests: endpoint.requests.length,
                    events: events
                      .slice(-20)
                      .map((event) =>
                        event.event === 'native'
                          ? event.data.name === 'connector.stage'
                            ? event.data.payload.stage
                            : event.data.name
                          : event.event,
                      ),
                  }),
              ),
            ),
          45000,
        );
      }),
    ]);
    assert.deepEqual(
      events.map((event) => event.seq),
      events.map((_, index) => index),
    );
    return {
      result,
      events,
      requests: endpoint.requests,
      output: existsSync(join(workspace, 'result.txt'))
        ? readFileSync(join(workspace, 'result.txt'), 'utf8')
        : null,
    };
  } catch (error) {
    error.message +=
      ' ' +
      JSON.stringify({
        requests: endpoint.requests.length,
        result: events.find((event) => event.event === 'run.finished')?.data,
        diagnostics: events
          .filter((event) => event.event === 'native' && event.data.name.startsWith('connector.'))
          .map((event) => event.data),
        last_events: events
          .slice(-12)
          .map((event) => (event.event === 'native' ? event.data.name : event.event)),
      });
    throw error;
  } finally {
    clearTimeout(timeout);
    await client.close(10000);
    await endpoint.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

test(
  'official OpenCode server completes with provider usage and no invented cost',
  { timeout: 120000 },
  async () => {
    const { result, events, requests } = await runFixture(
      (body, index, res, req) => {
        assert.equal(req.headers.authorization, 'Bearer fake-key-for-local-tests');
        assert.equal(body.temperature, 0.25);
        stream(res, index, { content: 'Completed' });
      },
      { config: { temperature: 0.25 } },
    );
    assert.equal(
      result.status,
      'success',
      JSON.stringify({
        result,
        requests: requests.length,
        diagnostics: events
          .filter((event) => event.event === 'native' && event.data.name.startsWith('connector.'))
          .map((event) => event.data),
      }),
    );
    assert.equal(requests.length, 1);
    const usage = events.filter((event) => event.event === 'usage').at(-1).data;
    assert.equal(usage.input_tokens, 10);
    assert.equal(usage.output_tokens, 2);
    assert.equal(usage.cache_read_tokens, 2);
    assert.equal(usage.reasoning_tokens, 1);
    assert.equal(usage.cost, null);
    assert.equal(usage.purpose, 'agent');
  },
);

test('official OpenCode executes a native file tool', { timeout: 120000 }, async () => {
  const { result, events, output } = await runFixture(
    (body, index, res, _req, workspace) => {
      assert.deepEqual(
        body.tools.map((tool) => tool.function.name),
        ['write'],
      );
      if (index === 1)
        stream(res, index, {
          tool: {
            id: 'write-1',
            type: 'function',
            function: {
              name: 'write',
              arguments: JSON.stringify({
                filePath: join(workspace, 'result.txt'),
                content: 'Engineering result\n',
              }),
            },
          },
        });
      else stream(res, index, { content: 'File created' });
    },
    { resources: [{ id: 'tools', kind: 'tools', config: { names: ['file_write'] } }] },
  );
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(output, 'Engineering result\n');
  const calls = events.filter((event) => event.event.startsWith('tool.'));
  assert(calls.some((event) => event.event === 'tool.start' && event.data.name === 'write'));
  assert(calls.some((event) => event.event === 'tool.end' && event.data.status === 'completed'));
});

test('OpenCode preserves missing provider usage as unknown', { timeout: 120000 }, async () => {
  const { result, events } = await runFixture((body, index, res) =>
    stream(res, index, { content: 'No usage', usage: false }),
  );
  assert.equal(result.status, 'success', JSON.stringify(result));
  const usage = events.filter((event) => event.event === 'usage').at(-1).data;
  assert.equal(usage.input_tokens, null);
  assert.equal(usage.output_tokens, null);
  assert.equal(usage.cache_read_tokens, null);
  assert.equal(usage.cost, null);
});

test('OpenCode empty tools and memory off suppress native discovery', { timeout: 120000 }, async () => {
  const { result } = await runFixture(
    (body, index, res) => {
      assert.equal(body.tools?.length ?? 0, 0);
      assert(!JSON.stringify(body.messages).includes('FORBIDDEN_DISCOVERED_MEMORY'));
      stream(res, index, { content: 'No tools or memory' });
    },
    {
      prepare(workspace) {
        writeFileSync(join(workspace, 'AGENTS.md'), 'FORBIDDEN_DISCOVERED_MEMORY');
        return [
          { id: 'tools', kind: 'tools', config: { names: [] } },
          { id: 'memory', kind: 'memory', config: { mode: 'off' } },
        ];
      },
    },
  );
  assert.equal(result.status, 'success', JSON.stringify(result));
});

test('OpenCode stdio MCP keeps native calls and canonical aliases', { timeout: 120000 }, async () => {
  const { result, events } = await runFixture(
    (body, index, res) => {
      assert.deepEqual(
        body.tools.map((tool) => tool.function.name),
        ['fixture_echo'],
      );
      if (index === 1)
        stream(res, index, {
          tool: {
            id: 'mcp-1',
            type: 'function',
            function: {
              name: 'fixture_echo',
              arguments: JSON.stringify({ text: 'fixture-value' }),
            },
          },
        });
      else {
        assert(JSON.stringify(body.messages).includes('fixture-value'));
        stream(res, index, { content: 'MCP complete' });
      }
    },
    {
      resources: [
        { id: 'tools', kind: 'tools', config: { names: ['search_knowledge_base'] } },
        {
          id: 'fixture',
          kind: 'mcp',
          config: {
            transport: 'stdio',
            command: process.execPath,
            args: [fileURLToPath(new URL('../../shared/fake-mcp.mjs', import.meta.url))],
            aliases: { echo: 'search_knowledge_base' },
          },
        },
      ],
    },
  );
  assert.equal(result.status, 'success', JSON.stringify(result));
  const call = events.find((event) => event.event === 'tool.start');
  assert.equal(call.data.name, 'fixture_echo');
  assert.deepEqual(call.data.aliases, ['search_knowledge_base']);
});

test('OpenCode HTTP MCP resolves private authorization', { timeout: 120000 }, async () => {
  const mcp = await fakeMcpHTTP((req) =>
    assert.equal(req.headers.authorization, 'Bearer fixture-knowledge-token'),
  );
  try {
    const { result } = await runFixture(
      (body, index, res) => {
        if (index === 1)
          stream(res, index, {
            tool: {
              id: 'knowledge-1',
              type: 'function',
              function: {
                name: 'knowledge_search_knowledge_base',
                arguments: JSON.stringify({ query: 'fixture' }),
              },
            },
          });
        else {
          assert(JSON.stringify(body.messages).includes('Knowledge fixture result'));
          stream(res, index, { content: 'Knowledge read' });
        }
      },
      {
        privateCredentials: { knowledge: 'Bearer fixture-knowledge-token' },
        resources: [
          { id: 'tools', kind: 'tools', config: { names: ['search_knowledge_base'] } },
          {
            id: 'knowledge',
            kind: 'mcp',
            config: {
              transport: 'http',
              url: mcp.url,
              aliases: { search_knowledge_base: 'search_knowledge_base' },
            },
            credential_refs: { 'headers.Authorization': 'knowledge' },
          },
        ],
      },
    );
    assert.equal(result.status, 'success', JSON.stringify(result));
  } finally {
    await mcp.close();
  }
});

test('OpenCode provider authentication failure stays a failed attempt', { timeout: 120000 }, async () => {
  const { result, requests } = await runFixture((body, index, res) => {
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'Fixture authentication failure' } }));
  });
  assert.equal(result.status, 'failed');
  assert.equal(requests.length, 1);
  assert.equal(result.error.details.normalized_failure.kind, 'model_error');
  assert.equal(result.error.details.normalized_failure.status_code, 401);
});

async function waitFor(predicate, description) {
  const deadline = Date.now() + 35000;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error('Timed out waiting for ' + description);
}

test(
  'OpenCode feedback persists before its acknowledgement and final settlement',
  { timeout: 120000 },
  async () => {
    let release;
    const { result, requests } = await runFixture(
      async (body, index, res) => {
        if (index === 1)
          await new Promise((resolve) => {
            release = () => {
              stream(res, index, { content: 'First response' });
              resolve();
            };
          });
        else {
          assert(JSON.stringify(body.messages).includes('Add the fixture note'));
          stream(res, index, { content: 'Feedback incorporated' });
        }
      },
      {
        async control(client, endpoint, spec) {
          await endpoint.waitForRequest();
          try {
            const receipt = await client.request(
              'control',
              { attempt_id: spec.attempt_id, command: 'feedback', payload: { text: 'Add the fixture note' } },
              15000,
            );
            assert.equal(receipt.accepted, true);
          } finally {
            release?.();
          }
        },
      },
    );
    assert.equal(result.status, 'success', JSON.stringify(result));
    assert.equal(result.summary, 'Feedback incorporated');
    assert.equal(requests.length, 2);
  },
);

test('OpenCode cancellation interrupts a live provider stream', { timeout: 120000 }, async () => {
  const { result } = await runFixture(
    (body, index, res) => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write(
        'data: ' +
          JSON.stringify({
            id: 'pending',
            object: 'chat.completion.chunk',
            model: 'fake-model',
            choices: [{ index: 0, delta: { role: 'assistant', content: 'Working' }, finish_reason: null }],
          }) +
          '\n\n',
      );
    },
    {
      async control(client, endpoint, spec) {
        await endpoint.waitForRequest();
        assert.equal(
          (await client.request('control', { attempt_id: spec.attempt_id, command: 'abort' })).accepted,
          true,
        );
      },
    },
  );
  assert.equal(result.status, 'cancelled', JSON.stringify(result));
});

test('OpenCode cancellation stops a running native bash child', { timeout: 120000 }, async () => {
  let pid;
  const { result } = await runFixture(
    (body, index, res) => {
      stream(res, index, {
        tool: {
          id: 'sleep-1',
          type: 'function',
          function: {
            name: 'bash',
            arguments: JSON.stringify({
              command: 'sleep 30 & echo $! > child.pid; wait',
              description: 'Wait for cancellation',
            }),
          },
        },
      });
    },
    {
      resources: [{ id: 'tools', kind: 'tools', config: { names: ['bash'] } }],
      async control(client, endpoint, spec) {
        await endpoint.waitForRequest();
        await waitFor(() => existsSync(join(spec.cwd, 'child.pid')), 'native child');
        pid = Number(readFileSync(join(spec.cwd, 'child.pid'), 'utf8').trim());
        assert.equal(
          (await client.request('control', { attempt_id: spec.attempt_id, command: 'abort' })).accepted,
          true,
        );
      },
    },
  );
  assert.equal(result.status, 'cancelled', JSON.stringify(result));
  await waitFor(() => {
    try {
      process.kill(pid, 0);
      return readFileSync(`/proc/${pid}/stat`, 'utf8').split(' ')[2] === 'Z';
    } catch {
      return true;
    }
  }, 'native child to stop');
});

test('OpenCode permission requests require an explicit native reply', { timeout: 120000 }, async () => {
  const { result, events } = await runFixture(
    (body, index, res) => {
      if (index === 1)
        stream(res, index, {
          tool: {
            id: 'approval-1',
            type: 'function',
            function: {
              name: 'bash',
              arguments: JSON.stringify({
                command: 'echo fixture-approved',
                description: 'Approval fixture',
              }),
            },
          },
        });
      else stream(res, index, { content: 'Approved operation completed' });
    },
    {
      config: { permissionMode: 'ask' },
      resources: [{ id: 'tools', kind: 'tools', config: { names: ['bash'] } }],
      async control(client, _endpoint, spec, events) {
        const request = await waitFor(
          () => events.find((event) => event.event === 'interaction.request'),
          'approval request',
        );
        assert.equal(
          (
            await client.request('control', {
              attempt_id: spec.attempt_id,
              command: 'approval',
              payload: { request_id: request.data.request_id, decision: 'once' },
            })
          ).accepted,
          true,
        );
      },
    },
  );
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(events.filter((event) => event.event === 'interaction.request').length, 1);
  assert.equal(events.filter((event) => event.event === 'interaction.resolved').length, 1);
});

test('OpenCode unsupported question interaction fails explicitly', { timeout: 120000 }, async () => {
  const { result } = await runFixture(
    (body, index, res) => {
      stream(res, index, {
        tool: {
          id: 'question-1',
          type: 'function',
          function: {
            name: 'question',
            arguments: JSON.stringify({
              questions: [
                {
                  question: 'Choose a fixture',
                  header: 'Fixture',
                  options: [
                    { label: 'A', description: 'First fixture' },
                    { label: 'B', description: 'Second fixture' },
                  ],
                },
              ],
            }),
          },
        },
      });
    },
    { resources: [{ id: 'tools', kind: 'tools', config: { names: ['question'] } }] },
  );
  assert.equal(result.status, 'failed', JSON.stringify(result));
  assert.equal(result.error.code, 'UNSUPPORTED_INTERACTION');
});

function interruptedPermissionStream() {
  let dropped = false;
  return async (request) => {
    const response = await fetch(request);
    if (dropped || new URL(request.url).pathname !== '/event' || !response.body) return response;
    const reader = response.body.getReader();
    return new Response(
      new ReadableStream({
        async pull(controller) {
          const value = await reader.read();
          if (value.done) {
            controller.close();
            return;
          }
          const text = new TextDecoder().decode(value.value);
          if (text.includes('permission.') && text.includes('.asked')) {
            dropped = true;
            await reader.cancel();
            controller.close();
            return;
          }
          controller.enqueue(value.value);
        },
        cancel() {
          return reader.cancel();
        },
      }),
      { status: response.status, headers: response.headers },
    );
  };
}

test(
  'OpenCode SSE reconnect recovers pending approval without duplicate tools or usage',
  { timeout: 120000 },
  async () => {
    const bootstrap = `
    import {readFileSync} from 'node:fs';
    import {serveStdio} from ${JSON.stringify(new URL('../../../sdk/typescript/dist/index.js', import.meta.url).href)};
    import {OpenCodeConnector} from ${JSON.stringify(new URL('../dist/connector.js', import.meta.url).href)};
    const credentials = JSON.parse(readFileSync(process.env.EVALHIVE_CREDENTIALS_FILE, 'utf8'));
    await serveStdio(new OpenCodeConnector(credentials, {fetch: (${interruptedPermissionStream.toString()})()}));
  `;
    const { result, events, requests } = await runFixture(
      (body, index, res) => {
        if (index === 1)
          stream(res, index, {
            tool: {
              id: 'reconnect-1',
              type: 'function',
              function: {
                name: 'bash',
                arguments: JSON.stringify({
                  command: 'echo fixture-reconnected',
                  description: 'Reconnect fixture',
                }),
              },
            },
          });
        else stream(res, index, { content: 'Recovered operation completed' });
      },
      {
        bootstrap,
        config: { permissionMode: 'ask' },
        resources: [{ id: 'tools', kind: 'tools', config: { names: ['bash'] } }],
        async control(client, _endpoint, spec, events) {
          const request = await waitFor(
            () => events.find((event) => event.event === 'interaction.request'),
            'recovered approval',
          );
          await client.request('control', {
            attempt_id: spec.attempt_id,
            command: 'approval',
            payload: { request_id: request.data.request_id, decision: 'once' },
          });
        },
      },
    );
    assert.equal(result.status, 'success', JSON.stringify(result));
    assert(result.metadata.recording.reconnects >= 1);
    assert.equal(events.filter((event) => event.event === 'interaction.request').length, 1);
    assert.equal(events.filter((event) => event.event === 'tool.start').length, 1);
    assert.equal(events.filter((event) => event.event === 'tool.end').length, 1);
    assert.equal(
      new Set(events.filter((event) => event.event === 'usage').map((event) => event.data.usage_id)).size,
      requests.length,
    );
  },
);

test(
  'an OpenCode server crash cannot leave a successful or running attempt',
  { timeout: 120000 },
  async () => {
    const { result } = await runFixture(
      (body, index, res) => {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        res.write(
          'data: ' +
            JSON.stringify({
              id: 'pending',
              object: 'chat.completion.chunk',
              model: 'fake-model',
              choices: [{ index: 0, delta: { role: 'assistant', content: 'Working' }, finish_reason: null }],
            }) +
            '\n\n',
        );
      },
      {
        async control(_client, endpoint, _spec, events) {
          await endpoint.waitForRequest();
          const ready = await waitFor(
            () =>
              events.find(
                (event) =>
                  event.event === 'native' &&
                  event.data.name === 'connector.stage' &&
                  event.data.payload.stage === 'server_ready',
              ),
            'server identity',
          );
          process.kill(ready.data.payload.pid, 'SIGKILL');
        },
      },
    );
    assert.equal(result.status, 'failed', JSON.stringify(result));
    assert(['harness_error', 'recording_error'].includes(result.error.details.normalized_failure.kind));
  },
);

test('OpenCode loads explicit SKILL.md and memory resources', { timeout: 120000 }, async () => {
  const { result } = await runFixture(
    (body, index, res) => {
      const context = JSON.stringify(body.messages) + JSON.stringify(body.tools);
      assert(context.includes('fixture-skill'));
      assert(context.includes('REMEMBER_FIXTURE_CONTEXT'));
      stream(res, index, { content: 'Configured resources' });
    },
    {
      prepare(workspace) {
        const root = join(workspace, 'fixture-skill');
        mkdirSync(root);
        const path = join(root, 'SKILL.md');
        writeFileSync(
          path,
          '---\nname: fixture-skill\ndescription: Read the engineering fixture.\n---\nUse fixture data.\n',
        );
        return [
          { id: 'skill', kind: 'skill', config: { path } },
          {
            id: 'memory',
            kind: 'memory',
            config: { mode: 'structured', content: 'REMEMBER_FIXTURE_CONTEXT' },
          },
        ];
      },
    },
  );
  assert.equal(result.status, 'success', JSON.stringify(result));
});

test('OpenCode turn budget stops before another model request', { timeout: 120000 }, async () => {
  const { result, requests } = await runFixture(
    (body, index, res, _req, workspace) => {
      stream(res, index, {
        tool: {
          id: `write-${index}`,
          type: 'function',
          function: {
            name: 'write',
            arguments: JSON.stringify({ filePath: join(workspace, 'result.txt'), content: 'Budget fixture' }),
          },
        },
      });
    },
    { limits: { max_turns: 1 } },
  );
  assert.equal(result.status, 'failed', JSON.stringify(result));
  assert.equal(result.error.code, 'TURN_LIMIT');
  assert.equal(result.error.details.normalized_failure.kind, 'limit');
  assert.equal(requests.length, 1);
});

test('OpenCode native provider retry stays within the same turn budget', { timeout: 120000 }, async () => {
  const { result, requests } = await runFixture(
    (body, index, res) => {
      if (index === 1) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: { message: 'Temporary fixture failure' } }));
      } else stream(res, index, { content: 'Recovered' });
    },
    { limits: { max_turns: 1 } },
  );
  assert.equal(result.status, 'success', JSON.stringify(result));
  assert.equal(requests.length, 2);
});
