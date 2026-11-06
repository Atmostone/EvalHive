import {Context, Peer} from './peer.js';
import {ProtocolError, validate, validateSpec, validateEnvironment, type JsonObject, type JsonValue,
  type Manifest, type Scope, type TrialSpec, type WireMessage} from './protocol.js';

export const RESULT_TYPES: Record<string, string> = {
  describe: 'manifest', list_cases: 'case_page', resolve_case: 'case_descriptor',
  validate: 'validation_result', plan: 'environment_plan', prepare: 'prepared',
  evaluate: 'evaluation_result', aggregate: 'aggregate_result',
};
export interface Connector {
  manifest: Manifest;
  handle(method: string, params: JsonObject, context: Context): Promise<JsonValue>;
  close?(context: Context): Promise<void>;
}
export async function serve(connector: Connector, input: AsyncIterable<Uint8Array>, write: (frame: Buffer) => Promise<void>,
  limits: {maxPending?: number; maxActive?: number; receiptLimit?: number} = {}): Promise<void> {
  validate('manifest', connector.manifest);
  let context: Context;
  let purpose: 'execution' | 'evaluation' | undefined;
  const handler = async (request: WireMessage): Promise<JsonValue> => {
    const method = String(request.method), params = request.params as JsonObject;
    if (method === 'agent.call') throw new ProtocolError('WRONG_DIRECTION', 'Agent calls target the host', 'configuration');
    if (method === 'describe') return connector.manifest;
    if (method === 'shutdown') {context.controller.abort(); return {stopped: true};}
    if ('spec' in params) {
      const spec = validateSpec(connector.manifest, params.spec as TrialSpec);
      const selected = spec.purpose ?? 'execution';
      if (purpose !== undefined && purpose !== selected)
        throw new ProtocolError('PURPOSE_CONFLICT', 'A worker cannot change its execution purpose', 'configuration');
      purpose = selected;
      context.purpose = selected;
      context.agentEnabled = [...((connector.manifest.requirements.capabilities ?? []) as string[]),
        ...(((spec.case.requirements as JsonObject).capabilities ?? []) as string[])].includes('host.agent_api_v1');
    }
    if ('plan' in params) validateEnvironment(params.spec as TrialSpec, params.plan as JsonObject);
    if (method === 'drive' && purpose === 'evaluation')
      throw new ProtocolError('EVALUATION_ONLY', 'An evaluation worker cannot drive an agent', 'unsupported');
    if (['prepare', 'drive', 'capture', 'evaluate', 'cleanup', 'cancel'].includes(method))
      context.bind('spec' in params ? (params.spec as TrialSpec).scope : params.scope as Scope);
    if (['prepare', 'drive'].includes(method) && context.signal.aborted)
      throw new ProtocolError('CANCELLED', 'Trial was cancelled');
    if (method === 'cancel') context.controller.abort();
    if (['prepare', 'drive', 'capture', 'evaluate', 'aggregate', 'cleanup'].includes(method) &&
        !['native', 'emulated'].includes(connector.manifest.capabilities[method] ?? 'unsupported'))
      throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Connector capability is unavailable');
    const result = await connector.handle(method, params, context);
    if (RESULT_TYPES[method]) validate(RESULT_TYPES[method], result);
    if (method === 'plan' || method === 'prepare')
      validateEnvironment(params.spec as TrialSpec, result as JsonObject, method === 'plan' ? 'environment_plan' : 'prepared');
    return result;
  };
  const peer = new Peer(write, {handler, ...limits});
  context = new Context(peer);
  try {await peer.read(input);}
  finally {context.controller.abort(); await connector.close?.(context);}
}
export async function serveStdio(connector: Connector): Promise<void> {
  await serve(connector, process.stdin, frame => new Promise<void>((resolve, reject) => {
    process.stdout.write(frame, error => error ? reject(error) : resolve());
  }));
}
