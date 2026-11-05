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
    if (method === 'plan') return fixture.plan;
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
      return {ready: true, session_handle: result.handle, agent: fixture.plan.agent, native_state: {fixture: true}, artifacts: []};
    }
    if (method === 'drive') {
      await context.emit(params.scope, 'agent.request', {input_id: 'turn-1', text: 'Fixture input'});
      return {finished: true};
    }
    if (method === 'capture') return {artifacts: [], regrade: 'available'};
    if (method === 'evaluate') return fixture.evaluation;
    if (method === 'aggregate') return {metrics: [{name: 'success_rate', value: 1}], expected: 1, observed: 1, native: {}};
    if (method === 'cleanup') return {cleaned: true};
    if (method === 'cancel') return {cancelled: true};
    throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Unknown operation');
  },
});
