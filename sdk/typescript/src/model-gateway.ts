import {createServer, request, type IncomingHttpHeaders, type ServerResponse} from 'node:http';
import {Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {ProtocolError, validate, type JsonObject, type RunSpec} from './protocol.js';

export interface ModelGatewayBinding {
  endpoint: string;
  close(): Promise<void>;
}

function headers(source: IncomingHttpHeaders): IncomingHttpHeaders {
  const excluded = new Set(['host', 'connection', 'proxy-connection', 'proxy-authorization',
    'keep-alive', 'transfer-encoding', 'upgrade', 'trailer', 'te', 'content-length']);
  for (const name of String(source.connection ?? '').split(',')) excluded.add(name.trim().toLowerCase());
  return Object.fromEntries(Object.entries(source).filter(([name]) => !excluded.has(name.toLowerCase())));
}

function bounded(maximum: number): Transform {
  let bytes = 0;
  return new Transform({transform(chunk, _encoding, callback) {
    bytes += chunk.length;
    callback(bytes > maximum ? new Error('Model gateway byte limit exceeded') : null, chunk);
  }});
}

/** Adapt native HTTP clients to an attempt-owned Unix channel without changing bodies. */
export async function openModelGateway(spec: RunSpec, signal: AbortSignal): Promise<ModelGatewayBinding | undefined> {
  const descriptor = (spec.metadata as JsonObject | undefined)?.model_gateway as JsonObject | undefined;
  if (descriptor === undefined) return undefined;
  validate('model_gateway', descriptor);
  if (signal.aborted) throw new ProtocolError('MODEL_GATEWAY_CANCELLED', 'Model channel was cancelled');
  const outgoing = new Set<ReturnType<typeof request>>();
  let closed = false;
  const fail = (response: ServerResponse) => {
    if (response.destroyed) return;
    if (response.headersSent) response.destroy();
    else response.writeHead(502, {'content-type': 'application/json'}).end(
      JSON.stringify({error: {code: 'MODEL_GATEWAY_UNAVAILABLE', message: 'Owned model channel failed'}}));
  };
  const server = createServer({maxHeaderSize: 16384, requestTimeout: 0}, (incoming, response) => {
    if (closed || outgoing.size >= 8) {response.writeHead(503).end(); return;}
    if (incoming.method !== 'POST' || !['/v1/chat/completions', '/v1/responses'].includes(incoming.url ?? '')) {
      response.writeHead(404).end(); return;
    }
    const upstream = request({socketPath: descriptor.socket_path as string,
      path: '/' + descriptor.role + incoming.url, method: 'POST', headers: headers(incoming.headers), signal}, received => {
      response.writeHead(received.statusCode ?? 502, headers(received.headers));
      void pipeline(received, bounded(64 * 1024 ** 2), response).catch(() => fail(response));
    });
    outgoing.add(upstream);
    upstream.on('close', () => outgoing.delete(upstream));
    upstream.on('error', () => fail(response));
    response.on('close', () => upstream.destroy());
    void pipeline(incoming, bounded(2 * 1024 ** 2), upstream).catch(() => {upstream.destroy(); fail(response);});
  });
  let closing: Promise<void> | undefined;
  const close = (): Promise<void> => closing ??= (async () => {
    closed = true;
    signal.removeEventListener('abort', abort);
    for (const connection of outgoing) connection.destroy();
    server.closeAllConnections();
    if (server.listening) await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  })();
  const abort = () => {void close();};
  try {
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', () => {server.removeListener('error', reject); resolve();});
    });
    signal.addEventListener('abort', abort, {once: true});
    if (signal.aborted) {await close(); throw new ProtocolError('MODEL_GATEWAY_CANCELLED', 'Model channel was cancelled');}
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('Model gateway address unavailable');
    return {endpoint: `http://127.0.0.1:${address.port}/v1`, close};
  } catch (error) {await close(); throw error;}
}
