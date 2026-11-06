// External benchmark package; it imports no backend or model client.
import {readFileSync} from 'node:fs';
import {serveStdio, ProtocolError} from '@evalhive/benchmark-sdk';
const fixture = JSON.parse(readFileSync(new URL('./example.json', import.meta.url), 'utf8'));
await serveStdio({
  manifest: fixture.manifest,
  async handle(method, params, context) {
    if (method === 'list_cases') return {cases: [fixture.case], next_cursor: null};
    if (method === 'resolve_case') return fixture.case;
    if (method === 'validate') return {accepted: true, effective_config: {prefix: '', ...params.spec.config}};
    if (method === 'plan') {
      if (params.spec.purpose === 'evaluation') {
        const {agent, ...plan} = fixture.plan;
        return {...plan, purpose:'evaluation'};
      }
      return fixture.plan;
    }
    if (method === 'prepare') {
      if (params.spec.config.wait) {
        await new Promise(resolve => {
          if (context.signal.aborted) resolve();
          else context.signal.addEventListener('abort', resolve, {once: true});
        });
        throw new ProtocolError('CANCELLED', 'Preparation cancelled');
      }
      const result = await context.broker(params.spec.scope, 'example.provider', 'allocate',
        {resource: params.plan.resources[0]}, {requestId: 'example-allocate'});
      await context.emit(params.spec.scope, 'environment.ready', {session_handle: result.handle});
      return {ready: true, session_handle: result.handle, artifacts: [],
        ...(params.spec.purpose === 'evaluation' ?
          {purpose:'evaluation',native_state:params.spec.checkpoint.state} :
          {agent:fixture.plan.agent,native_state:{fixture:true}})};
    }
    if (method === 'drive') {
      if (params.payload.agent_operation) {
        const options = {requestId:params.payload.agent_request_id ?? 'fixture-agent-call', ...(params.payload.agent_session ? {sessionId:params.payload.agent_session} : {})};
        let result = await context.agent(params.scope, params.payload.agent_operation, params.payload.agent_payload ?? {}, options);
        if (params.payload.repeat_agent)
          result = await context.agent(params.scope, params.payload.agent_operation, params.payload.agent_payload ?? {}, options);
        return {finished:true, agent:result};
      }
      await context.emit(params.scope, 'agent.request', {input_id: 'turn-1', text: 'Fixture input'});
      return {finished: true};
    }
    if (method === 'capture') return {artifacts: [], regrade: 'available', checkpoint:fixture.checkpoint};
    if (method === 'evaluate') {
      if (params.payload.agent_operation) await context.agent(params.scope, 'run', {});
      return fixture.evaluation;
    }
    if (method === 'aggregate') return {metrics: [{name: 'success_rate', value: 1}], expected: 1, observed: 1, native: {}};
    if (method === 'cleanup') return {cleaned: true};
    if (method === 'cancel') return {cancelled: true};
    throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Unknown operation');
  },
});
