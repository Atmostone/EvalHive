import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {ConnectorClient, Peer, command} from '../dist/index.js';

const fixture = JSON.parse(readFileSync(new URL('../../fixtures/conformance.json', import.meta.url), 'utf8'));
fixture.spec.case.requirements.capabilities = [...(fixture.spec.case.requirements.capabilities ?? []), 'host.agent_api_v1'];
const python = process.env.EH_TEST_PYTHON ?? 'python3';
for (const language of ['python', 'typescript']) {
  const argv = language === 'python' ? [python, fileURLToPath(new URL('../../examples/fake/connector.py', import.meta.url))] :
    [process.execPath, fileURLToPath(new URL('../../examples/fake/connector.mjs', import.meta.url))];
  test(`agent and resource calls share one transport, retired receipts are read: ${language}`, async () => {
    let calls = 0, replays = 0, resources = 0;
    const answer = {session_id:'session-one', status:'failed', outcome:{attempt_id:'attempt-one',evidence_digest:'a'.repeat(64)}};
    const client = ConnectorClient.launch(argv, {
      agent: async request => {assert.deepEqual(request.params.scope, fixture.spec.scope); calls++; return answer;},
      agentReplay: async () => {replays++; return answer;},
      broker: async () => {resources++; return {handle:'prepared-environment'};},
    });
    try {
      await client.request('prepare', {spec:fixture.spec, plan:fixture.plan});
      const reply = await client.request('drive', {scope:fixture.spec.scope, payload:{agent_operation:'run',repeat_agent:true}});
      assert.deepEqual(reply.agent, answer);
      assert.deepEqual([calls, replays, resources], [1, 1, 1]);
      assert.equal((await client.request('describe')).id, 'example.benchmark');
    } finally {await client.close();}
  });
  test(`missing retired agent receipt cannot repeat an effect: ${language}`, async () => {
    let calls = 0;
    const client = ConnectorClient.launch(argv, {agent:async () => {calls++; return {session_id:'session-one',status:'success',outcome:{attempt_id:'attempt-one',evidence_digest:'a'.repeat(64)}};}});
    try {
      await client.request('validate', {spec:fixture.spec});
      await assert.rejects(client.request('drive', {scope:fixture.spec.scope, payload:{agent_operation:'run',repeat_agent:true}}),
        error => error.code === 'RECEIPT_UNAVAILABLE' && error.kind === 'uncertain');
      assert.equal(calls, 1);
    } finally {await client.close();}
  });
  test(`cancellation interrupts a pending agent call: ${language}`, async () => {
    let entered;
    const started = new Promise(resolve => {entered = resolve;});
    const client = ConnectorClient.launch(argv, {agent:async () => {entered(); return new Promise(() => {});}});
    try {
      await client.request('validate', {spec:fixture.spec});
      const pending = client.request('drive', {scope:fixture.spec.scope, payload:{agent_operation:'run'}});
      const cancelled = assert.rejects(pending, error => error.code === 'CANCELLED');
      await started;
      await client.request('cancel', {scope:fixture.spec.scope, payload:{}});
      await cancelled;
    } finally {await client.close();}
  });
  test(`evaluation and wrong-direction requests cannot start an agent: ${language}`, async () => {
    let calls = 0;
    const client = ConnectorClient.launch(argv, {agent:async () => {calls++; return {};}});
    try {
      const spec = fixture.evaluation_spec;
      await client.request('validate', {spec});
      await assert.rejects(client.request('evaluate', {scope:spec.scope, payload:{agent_operation:'run'}}), error => error.code === 'AGENT_UNAVAILABLE');
      assert.equal(calls, 0);
      await assert.rejects(client.request('agent.call', {scope:spec.scope,operation:'run',payload:{}}), error => error.code === 'WRONG_DIRECTION');
    } finally {await client.close();}
  });
  test(`agent session controls, events and completion: ${language}`, async () => {
    const answers = {
      open:{session_id:'session-one',attempt_id:'attempt-one'},
      control:{session_id:'session-one',command_id:'command-one',result:{accepted:true}},
      events:{session_id:'session-one',events:[{event:'turn.completed'}],cursor:1,closed:false},
      close:{session_id:'session-one',status:'success',outcome:{attempt_id:'attempt-one',evidence_digest:'a'.repeat(64)}},
    };
    const client = ConnectorClient.launch(argv, {agent:async request => {
      if (request.params.operation !== 'open') assert.equal(request.params.session_id, 'session-one');
      return answers[request.params.operation];
    }});
    try {
      await client.request('validate', {spec:fixture.spec});
      for (const [operation, answer] of Object.entries(answers)) {
        const payload = {agent_operation:operation,agent_request_id:'request-'+operation,
          ...(operation !== 'open' ? {agent_session:'session-one'} : {}),
          ...(operation === 'control' ? {agent_payload:{name:'turn.submit',payload:{text:'Fixture'}}} : {}),
          ...(operation === 'events' ? {agent_payload:{cursor:0}} : {})};
        assert.deepEqual((await client.request('drive', {scope:fixture.spec.scope,payload})).agent, answer);
      }
    } finally {await client.close();}
  });
  for (const wrong of ['shape','session','cursor']) test(`invalid agent response ${wrong}: ${language}`, async () => {
    const client = ConnectorClient.launch(argv, {agent:async () => wrong === 'shape' ? {finished:true} :
      {session_id:wrong === 'session' ? 'other-session' : 'session-one',events:[],cursor:0,closed:false}});
    try {
      await client.request('validate', {spec:fixture.spec});
      await assert.rejects(client.request('drive', {scope:fixture.spec.scope,payload:{agent_operation:'events',agent_session:'session-one',agent_payload:{cursor:wrong === 'cursor' ? 1 : 0}}}),
        error => error.code === 'AGENT_REPLY_INVALID' && error.kind === 'recording');
    } finally {await client.close();}
  });
  test(`agent capability is required before a host call: ${language}`, async () => {
    let calls = 0;
    const client = ConnectorClient.launch(argv, {agent:async () => {calls++; return {};}});
    const spec = structuredClone(fixture.spec);
    spec.case.requirements.capabilities = spec.case.requirements.capabilities.filter(value => value !== 'host.agent_api_v1');
    try {
      await client.request('validate', {spec});
      await assert.rejects(client.request('drive', {scope:spec.scope,payload:{agent_operation:'run'}}), error => error.code === 'HOST_CAPABILITY_REQUIRED');
      assert.equal(calls, 0);
    } finally {await client.close();}
  });
}

test('request deadline includes blocked transport writes', {timeout:2000}, async () => {
  const peer = new Peer(() => new Promise(() => {}));
  await assert.rejects(peer.request(command('blocked', 'shutdown'), 20), error => error.code === 'COMMAND_UNCERTAIN');
});
