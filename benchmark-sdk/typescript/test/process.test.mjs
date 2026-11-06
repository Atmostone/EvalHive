import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {ConnectorClient, ProtocolError} from '../dist/index.js';
const fixture = JSON.parse(readFileSync(new URL('../../fixtures/conformance.json', import.meta.url), 'utf8'));
const python = process.env.EH_TEST_PYTHON ?? 'python3';
for (const language of ['python','typescript']) {
  const argv = language === 'python' ? [python,fileURLToPath(new URL('../../examples/fake/connector.py',import.meta.url))] :
    [process.execPath,fileURLToPath(new URL('../../examples/fake/connector.mjs',import.meta.url))];
  test(`native process lifecycle and reverse broker: ${language}`, async () => {
    let allocations = 0; const events = [];
    const client = ConnectorClient.launch(argv,{broker:async request => {
      assert.deepEqual(request.scope,fixture.spec.scope); allocations++;
      return {handle:'environment-example'};
    }});
    const reading = (async () => {for await (const event of client.events()) events.push(event);})();
    try {
      assert.equal((await client.request('describe')).id,'example.benchmark');
      assert.equal((await client.request('list_cases',{dataset_id:'example',revision:'1'})).cases.length,1);
      const plan = await client.request('plan',{spec:fixture.spec});
      const params = {spec:fixture.spec,plan};
      const prepared = await client.request('prepare',params,{requestId:'prepare-1'});
      assert.equal(prepared.ready,true);
      assert.deepEqual(await client.request('prepare',params,{requestId:'prepare-1'}),prepared);
      assert.equal(allocations,1);
      const changed = structuredClone(params);changed.spec.config.prefix='changed';
      await assert.rejects(client.request('prepare',changed,{requestId:'prepare-1'}),error => error instanceof ProtocolError && error.code==='COMMAND_CONFLICT');
      const scoped = {scope:fixture.spec.scope,payload:{}};
      assert.equal((await client.request('drive',scoped)).finished,true);
      assert.equal((await client.request('evaluate',scoped)).success,true);
      assert.equal((await client.request('cleanup',scoped)).cleaned,true);
      await assert.rejects(client.request('cleanup',{scope:{...scoped.scope,generation:2},payload:{}}),error=>error.code==='SCOPE_CONFLICT');
    } finally {await client.close();await reading;}
    assert.deepEqual(events.map(event=>event.seq),[0,1]);
  });
  test(`cancel pending operation: ${language}`, async () => {
    const client = ConnectorClient.launch(argv);
    const spec = structuredClone(fixture.spec);spec.config.wait=true;
    const pending = client.request('prepare',{spec,plan:fixture.plan});
    // Attach the rejection assertion before cancellation can settle the request.
    const cancelled = assert.rejects(pending,error=>error.code==='CANCELLED');
    try {
      await client.request('describe');
      assert.equal((await client.request('cancel',{scope:spec.scope,payload:{}})).cancelled,true);
      await cancelled;
    } finally {await client.close();}
  });
}

for (const language of ['python','typescript']) {
  const argv = language === 'python' ? [python,fileURLToPath(new URL('../../examples/fake/connector.py',import.meta.url))] :
    [process.execPath,fileURLToPath(new URL('../../examples/fake/connector.mjs',import.meta.url))];
  test(`durable wire error receipt: ${language}`, async () => {
    const client = ConnectorClient.launch(argv, {broker: async () => {
      throw new ProtocolError('REMOTE_UNCERTAIN', 'Private fixture error', 'uncertain');
    }});
    try {
      const receipt = await client.requestReceipt('describe', {}, {requestId:'full-description'});
      assert.equal(receipt.id,'full-description'); assert.equal(receipt.ok,true);
      const error = await client.requestReceipt('prepare',{spec:fixture.spec,plan:fixture.plan},{requestId:'full-error'});
      assert.equal(error.ok,false); assert.equal(error.error.code,'REMOTE_UNCERTAIN');
      assert.equal(error.error.kind,'uncertain');
    } finally {await client.close();}
  });
}
