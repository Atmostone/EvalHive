import {Context, Peer} from './peer.js';
import {ProtocolError, validate, type Manifest, type Scope, type JsonObject, type JsonValue, type WireMessage} from './protocol.js';

export interface ResourceRequest {
  protocol_version: '1.0'; type: 'broker_request'; id: string;
  scope: Scope; provider: string; operation: string; payload: JsonObject; handle?: string;
}
export interface EnvironmentProvider {
  manifest: Manifest;
  handle(request: ResourceRequest, context: Context): Promise<JsonValue>;
  close?(context: Context): Promise<void>;
}
export async function serveProvider(provider: EnvironmentProvider, input: AsyncIterable<Uint8Array>,
  write: (frame: Buffer) => Promise<void>, limits: {maxPending?: number; maxActive?: number; receiptLimit?: number} = {}): Promise<void> {
  validate('manifest', provider.manifest);
  if (provider.manifest.kind !== 'environment_provider') throw new ProtocolError('INVALID_MANIFEST', 'Expected an environment-provider manifest', 'configuration');
  let context: Context;
  const handler = async (request: WireMessage): Promise<JsonValue> => {
    if (request.type === 'command') {
      const params = request.params as JsonObject;
      if (request.method === 'describe') return provider.manifest;
      if (request.method === 'shutdown') {context.controller.abort(); return {stopped: true};}
      if (request.method === 'cancel') {context.bind(params.scope as Scope); context.controller.abort(); return {accepted: true};}
      throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Provider command is unavailable', 'unsupported');
    }
    if (request.provider !== provider.manifest.id) throw new ProtocolError('PROVIDER_MISMATCH', 'Resource operation targets another provider', 'configuration');
    context.bind(request.scope as Scope);
    const operation = String(request.operation);
    if (!['native', 'emulated'].includes(provider.manifest.capabilities[operation] ?? 'unsupported'))
      throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Provider operation is unavailable', 'unsupported');
    if (['allocate', 'exec'].includes(operation) && context.signal.aborted) throw new ProtocolError('CANCELLED', 'Provider generation was cancelled', 'execution');
    return provider.handle(validate<ResourceRequest>('broker_request', request), context);
  };
  const peer = new Peer(write, {handler, requestTypes: ['command', 'broker_request'], ...limits});
  context = new Context(peer);
  try {await peer.read(input);} finally {context.controller.abort(); await provider.close?.(context);}
}
export async function serveProviderStdio(provider: EnvironmentProvider): Promise<void> {
  await serveProvider(provider, process.stdin, frame => new Promise<void>((resolve, reject) => {
    process.stdout.write(frame, error => error ? reject(error) : resolve());
  }));
}
