import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {validate, validateSpec, validateConfig, command, encodeLine, decodeLine, ProtocolError} from '../dist/index.js';

const fixture = JSON.parse(readFileSync(new URL('../../fixtures/conformance.json', import.meta.url), 'utf8'));
for (const example of fixture.cases) test(example.name, () => {
  if (example.error) assert.throws(() => validate(example.kind, example.value), error => error instanceof ProtocolError && error.code === example.error);
  else assert.deepEqual(validate(example.kind, example.value), example.value);
});
test('portable JSON and LF framing', () => {
  const message = command('unicode', 'shutdown', {reason: 'Text\u2028🌍\u2029\n'});
  assert.deepEqual(decodeLine(encodeLine(message)), message);
  for (const frame of [Buffer.from('{}'), Buffer.from('{}\n{}\n'), Buffer.from([255, 10])])
    assert.throws(() => decodeLine(frame), ProtocolError);
  for (const result of [NaN, Infinity, 2 ** 53, '\ud800', undefined, new Date()])
    assert.throws(() => encodeLine({protocol_version:'1.0',type:'response',id:'x',ok:true,result}), ProtocolError);
});
test('configuration and credentials remain separate', () => {
  const spec = structuredClone(fixture.spec);
  validateSpec(fixture.manifest, spec);
  spec.config.api_key = 'private-example-value';
  assert.throws(() => validateSpec(fixture.manifest, spec), error => error.code === 'INVALID_CONFIG' && !error.message.includes('private-example-value'));
  delete spec.config.api_key;
  spec.models.user_simulator = {name:'example',endpoint:'https://example.invalid/v1',credential_ref:'private'};
  assert.throws(() => validateSpec(fixture.manifest, spec), error => error.code === 'CREDENTIAL_REQUIRED');
  spec.credential_refs.private = 'secret-version-1'; validateSpec(fixture.manifest, spec);
  for (const endpoint of ['https://user:secret@example.invalid/v1','https://example.invalid/v1?api_key=secret','file:///tmp/model']) {
    spec.models.user_simulator.endpoint = endpoint;
    assert.throws(() => validateSpec(fixture.manifest, spec), error => error.code === 'INVALID_CONFIG');
  }
});
test('remote configuration references are never resolved', () => {
  for (const schema of [{$ref:'https://example.invalid/schema'},{$defs:{unused:{$ref:'https://example.invalid/schema'}}},{type:'invented'}])
    assert.throws(() => validateConfig(schema, {}), error => error.code === 'INVALID_CONFIG');
});
test('environment graph and metric identities', () => {
  const plan = structuredClone(fixture.plan);
  plan.resources[0].depends_on = ['workspace'];
  assert.throws(() => validate('environment_plan', plan), error => error.code === 'RESOURCE_CYCLE');
  plan.resources[0].depends_on = ['absent'];
  assert.throws(() => validate('environment_plan', plan), error => error.code === 'MISSING_RESOURCE');
  const result = structuredClone(fixture.evaluation);
  result.metrics[0].value = null; validate('evaluation_result', result);
  result.metrics.push({...result.metrics[0]});
  assert.throws(() => validate('evaluation_result', result), error => error.code === 'DUPLICATE_METRIC');
});
