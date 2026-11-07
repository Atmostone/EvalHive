import assert from 'node:assert/strict';
import test from 'node:test';
import {nativePayload, validate} from '../dist/index.js';

test('native schema bounds retain explicit numeric identity and redactable strings', () => {
  const value = {tools: [{inputSchema: {maximum: 2 ** 63}, credential: 'fixture-string'}]};
  assert.throws(() => validate('event', {protocol_version: '1.0', type: 'event', event_id: 'event',
    attempt_id: 'attempt', seq: 0, source: 'fixture', event: 'native',
    data: {name: 'message_start', payload: value, adapter_version: 'fixture'}}));
  const encoded = nativePayload(value);
  assert.deepEqual(encoded, {encoding: 'evalhive.native-numbers.v1',
    value: {tools: [{inputSchema: {maximum: String(2 ** 63)}, credential: 'fixture-string'}]},
    number_paths: ['/tools/0/inputSchema/maximum']});
  validate('event', {protocol_version: '1.0', type: 'event', event_id: 'event',
    attempt_id: 'attempt', seq: 0, source: 'fixture', event: 'native',
    data: {name: 'message_start', payload: encoded, adapter_version: 'fixture'}});
  assert.equal(value.tools[0].inputSchema.maximum, 2 ** 63);
});

test('safe native payloads keep their shape and JSON pointers escape object keys', () => {
  assert.deepEqual(nativePayload({count: 0, fraction: 0.5, text: 'unchanged', optional: undefined}),
    {count: 0, fraction: 0.5, text: 'unchanged'});
  assert.deepEqual(nativePayload({'a/b~c': -1e20}).number_paths, ['/a~1b~0c']);
  assert.deepEqual(nativePayload(1e20).number_paths, ['']);
});
