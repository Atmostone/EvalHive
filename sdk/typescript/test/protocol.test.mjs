import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {validate, validateRun, response, encodeLine, decodeLine, lines, MAX_LINE_BYTES} from '../dist/index.js';

const fixtures = JSON.parse(readFileSync(new URL('../../fixtures/conformance.json', import.meta.url), 'utf8'));
for (const fixture of fixtures.cases) test(fixture.name, () => {
  if (fixture.error) assert.throws(() => validate(fixture.kind, fixture.value), {code: fixture.error});
  else {
    assert.deepEqual(validate(fixture.kind, fixture.value), fixture.value);
    if (fixture.kind === 'wire_message') assert.deepEqual(decodeLine(encodeLine(fixture.value)), fixture.value);
  }
});

test('configuration remains connector owned and is checked before start', () => {
  const manifest = structuredClone(fixtures.manifest), run = structuredClone(fixtures.run_spec);
  assert.deepEqual(validateRun(manifest, run), run);
  run.config.unsupported_native_flag = true;
  assert.throws(() => validateRun(manifest, run), {code: 'INVALID_CONFIG'});
  delete run.config.unsupported_native_flag;
  run.required_capabilities = ['approval'];
  assert.throws(() => validateRun(manifest, run), {code: 'UNSUPPORTED_CAPABILITY'});
});

test('owned model transport requires an explicit connector capability', () => {
  const manifest = structuredClone(fixtures.manifest), run = structuredClone(fixtures.run_spec);
  run.metadata = {model_gateway: {transport: 'unix_http_v1', socket_path: '/evalhive-control/model.sock', role: 'agent'}};
  assert.throws(() => validateRun(manifest, run), {code: 'UNSUPPORTED_CAPABILITY'});
  manifest.capabilities.model_gateway = 'native';
  assert.equal(validateRun(manifest, run), run);
});
for (const schema of [{$ref: 'https://invalid.example.test/schema'}, {type: 'invented'}, {$schema: 'http://json-schema.org/draft-07/schema#'}])
  test(`invalid configuration schema ${JSON.stringify(schema)}`, () => {
    assert.throws(() => validateRun({...fixtures.manifest, config_schema: schema}, fixtures.run_spec), {code: 'INVALID_CONFIG'});
  });

test('non portable values and cycles are rejected', () => {
  const cycle = []; cycle.push(cycle);
  let deep = []; for (let i = 0; i < 66; i++) deep = [deep];
  for (const value of [NaN, Infinity, 2n, undefined, '\ud800', [,,], new Date(), cycle, deep])
    assert.throws(() => response('r', value), {code: 'INVALID_MESSAGE'});
});

test('UTF8/LF framing preserves Unicode separators and splits byte chunks', async () => {
  const message = response('r', 'Hello\u2028🌍\u2029\n');
  const frame = encodeLine(message);
  async function* chunks() { for (const byte of frame) yield Buffer.from([byte]); }
  const decoded = []; for await (const line of lines(chunks())) decoded.push(decodeLine(line));
  assert.deepEqual(decoded, [message]);
  assert.deepEqual(decodeLine(Buffer.concat([frame.subarray(0, -1), Buffer.from('\r\n')])), message);
  for (const bad of [Buffer.from('{}'), Buffer.from('{}\n{}\n'), Buffer.from([255, 10]), Buffer.from('{"x":NaN}\n')])
    assert.throws(() => decodeLine(bad));
  assert.throws(() => encodeLine(response('r', 'x'.repeat(MAX_LINE_BYTES))), {code: 'FRAME_TOO_LARGE'});
  async function* incomplete() {yield Buffer.from('unterminated');}
  await assert.rejects(async () => {for await (const _ of lines(incomplete())) {}}, {code: 'INVALID_FRAME'});
});

test('resource resolution stays private and rejects duplicate singletons', async () => {
  const {resourceConfig, resources} = await import('../dist/index.js');
  const resource = {id: 'mcp', kind: 'mcp', config: {transport: 'http', url: 'http://fixture/mcp'},
    credential_refs: {'headers.Authorization': 'private'}};
  assert.equal(resourceConfig(resource, {private: 'Bearer fixture'}).headers.Authorization, 'Bearer fixture');
  assert.equal(resource.config.headers, undefined);
  assert.throws(() => resourceConfig(resource, {}), {code: 'CREDENTIAL_REQUIRED'});
  const duplicate = {id: 'tools', kind: 'tools', config: {names: []}};
  assert.throws(() => resources({environment: {resources: [duplicate, {...duplicate, id: 'other'}]}}), {code: 'DUPLICATE_RESOURCE'});
});
