import assert from 'node:assert/strict';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {ConnectorClient} from '../dist/index.js';

const scope = {workspace_id: 'workspace', trial_id: 'trial', generation: 1};
for (const language of ['python', 'typescript']) test(`provider operations and generation fencing: ${language}`, async () => {
  const entry = fileURLToPath(new URL(`../../examples/fake/provider.${language === 'python' ? 'py' : 'mjs'}`, import.meta.url));
  const client = ConnectorClient.launch([language === 'python' ? process.env.EH_TEST_PYTHON ?? 'python3' : process.execPath, entry]);
  const events = [];
  const reading = (async () => {for await (const event of client.events()) events.push(event);})();
  const call = (operation, payload = {}, options = {}) => client.resource(scope, 'example.provider', operation, payload, options);
  try {
    assert.equal((await client.request('describe')).kind, 'environment_provider');
    const allocated = await call('allocate', {kind: 'custom.fixture'}, {requestId: 'allocation'});
    assert.deepEqual(await call('allocate', {kind: 'custom.fixture'}, {requestId: 'allocation'}), allocated);
    await assert.rejects(call('allocate', {kind: 'changed'}, {requestId: 'allocation'}), error => error.code === 'COMMAND_CONFLICT');
    const handle = allocated.handle;
    assert.equal((await call('inspect', {}, {handle})).allocated, 1);
    await assert.rejects(client.resource({...scope, generation: 2}, 'example.provider', 'inspect', {}, {handle}), error => error.code === 'SCOPE_CONFLICT');
    await assert.rejects(client.resource(scope, 'other.provider', 'inspect', {}, {handle}), error => error.code === 'PROVIDER_MISMATCH');
    await assert.rejects(call('inspect', {}, {handle: 'foreign'}), error => error.code === 'FOREIGN_HANDLE');
    await assert.rejects(call('export', {}, {handle}), error => error.code === 'UNSUPPORTED_CAPABILITY');
    await assert.rejects(call('exec', {fail: true}, {handle}), error => error.code === 'CONNECTOR_ERROR' && !error.message.includes('private-native'));
    const pending = call('exec', {wait: true}, {handle});
    const cancelled = assert.rejects(pending, error => error.code === 'CANCELLED');
    await client.request('cancel', {scope, payload: {}}); await cancelled;
    await assert.rejects(call('allocate'), error => error.code === 'CANCELLED');
    assert.equal((await call('stop', {}, {handle})).stopped, true);
    assert.equal((await call('release', {}, {handle})).released, true);
    await client.peer.acknowledge('broker_request', 'allocation');
    await assert.rejects(call('allocate', {kind: 'custom.fixture'}, {requestId: 'allocation'}), error => error.code === 'RECEIPT_RETIRED');
  } finally {await client.close(); await reading;}
  assert.equal(events.length, 1); assert.equal(events[0].source, 'provider');
});
