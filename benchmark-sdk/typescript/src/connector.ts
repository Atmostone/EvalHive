import {Context, Peer} from './peer.js';
import {ProtocolError, validate, validateSpec, type JsonObject, type JsonValue,
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
  const handler = async (request: WireMessage): Promise<JsonValue> => {
    const method = String(request.method), params = request.params as JsonObject;
    if (method === 'describe') return connector.manifest;
    if (method === 'shutdown') {context.controller.abort(); return {stopped: true};}
    if ('spec' in params) validateSpec(connector.manifest, params.spec as TrialSpec);
    if ('plan' in params) validate('environment_plan', params.plan);
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
