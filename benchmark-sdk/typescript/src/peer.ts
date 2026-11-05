import {createHash, randomUUID} from 'node:crypto';
import {command, decodeLine, encodeLine, lines, ProtocolError,
  type ErrorKind, type JsonObject, type JsonValue, type Scope, type WireMessage} from './protocol.js';

type Handler = (request: WireMessage) => Promise<JsonValue>;
type Pending = {resolve: (value: JsonValue) => void; reject: (error: ProtocolError) => void};
const key = (type: string, id: string | null) => JSON.stringify([type, id]);
function stable(value: JsonValue): string {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + stable(value[k])).join(',') + '}';
  return JSON.stringify(value);
}

export class Peer {
  private pending = new Map<string, Pending>();
  private receipts = new Map<string, {signature: string; response?: Promise<WireMessage>; finished: boolean}>();
  private unacknowledged = 0;
  private active = new Set<Promise<WireMessage>>();
  private deliveries = new Set<Promise<void>>();
  private writing: Promise<void> = Promise.resolve();
  closed = false;
  constructor(private write: (frame: Buffer) => Promise<void>, private options: {
    handler?: Handler; requestType?: string; eventHandler?: (event: WireMessage) => Promise<void>;
    maxPending?: number; maxActive?: number; receiptLimit?: number; tombstoneLimit?: number;
  } = {}) {
    for (const n of [options.maxPending ?? 64, options.maxActive ?? 16, options.receiptLimit ?? 256, options.tombstoneLimit ?? 16384])
      if (!Number.isInteger(n) || n < 1) throw new Error('RPC limits must be positive integers');
  }
  async send(value: WireMessage): Promise<void> {
    const frame = encodeLine(value);
    const sent = this.writing.then(() => this.write(frame));
    this.writing = sent.catch(() => {});
    try {await sent;}
    catch (error) {
      if (error instanceof ProtocolError) throw error;
      throw new ProtocolError('TRANSPORT_CLOSED', 'Peer transport is closed');
    }
  }
  async request(value: WireMessage, timeout = 30_000): Promise<JsonValue> {
    if (this.closed) throw new ProtocolError('TRANSPORT_CLOSED', 'Peer is closed');
    if (this.pending.size >= (this.options.maxPending ?? 64)) throw new ProtocolError('PENDING_LIMIT', 'Pending request limit reached');
    encodeLine(value);
    const identity = key(value.type, value.id);
    if (this.pending.has(identity)) throw new ProtocolError('REQUEST_PENDING', 'Request already awaits a response');
    let timer: NodeJS.Timeout;
    const result = new Promise<JsonValue>((resolve, reject) => {
      timer = setTimeout(() => reject(new ProtocolError('COMMAND_UNCERTAIN', 'Request timed out; its outcome is unknown')), timeout);
      this.pending.set(identity, {resolve, reject});
    });
    // Observe a response failure even when writing the frame fails first.
    void result.catch(() => {});
    try {await this.send(value); return await result;}
    finally {clearTimeout(timer!); this.pending.delete(identity);}
  }
  async acknowledge(requestType: string, requestId: string): Promise<void> {
    await this.send({protocol_version: '1.0', type: 'receipt_ack', id: requestId, request_type: requestType});
  }
  private async answer(value: WireMessage): Promise<WireMessage> {
    const base = {protocol_version: '1.0', type: value.type === 'broker_request' ? 'broker_response' : 'response', id: value.id};
    try {
      const reply = {...base, ok: true, result: await this.options.handler!(value)};
      encodeLine(reply); return reply;
    } catch (error) {
      return {...base, ok: false, error: error instanceof ProtocolError ? error.asData() :
        {code: 'CONNECTOR_ERROR', message: 'Connector operation failed', kind: 'execution'}};
    }
  }
  private async dispatch(value: WireMessage): Promise<void> {
    const identity = key(value.type, value.id), signature = createHash('sha256').update(stable(value)).digest('hex'), previous = this.receipts.get(identity);
    if (previous) {
      if (previous.signature !== signature) {
        await this.send({protocol_version: '1.0', type: value.type === 'broker_request' ? 'broker_response' : 'response',
          id: value.id, ok: false, error: {code: 'COMMAND_CONFLICT', message: 'Request identity has different content', kind: 'configuration'}});
      } else if (previous.response) await this.send(await previous.response);
      else await this.send({protocol_version: '1.0', type: value.type === 'broker_request' ? 'broker_response' : 'response', id: value.id,
        ok: false, error: {code: 'RECEIPT_RETIRED', message: 'Read the acknowledged receipt from durable storage', kind: 'uncertain'}});
      return;
    }
    const full = this.unacknowledged >= (this.options.receiptLimit ?? 256) || this.receipts.size >= (this.options.tombstoneLimit ?? 16384);
    if (full || this.active.size >= (this.options.maxActive ?? 16)) {
      await this.send({protocol_version: '1.0', type: value.type === 'broker_request' ? 'broker_response' : 'response', id: value.id,
        ok: false, error: {code: full ? 'LEDGER_FULL' : 'BUSY',
          message: 'RPC capacity reached', kind: 'transport'}});
      return;
    }
    const response = Promise.resolve().then(() => this.answer(value));
    const receipt = {signature, response, finished: false};
    this.receipts.set(identity, receipt); this.unacknowledged++; this.active.add(response);
    void response.then(() => {receipt.finished = true;});
    try {await this.send(await response);} finally {this.active.delete(response);}
  }
  async read(stream: AsyncIterable<Uint8Array>): Promise<void> {
    let failure = new ProtocolError('TRANSPORT_CLOSED', 'Peer disconnected');
    try {
      for await (const line of lines(stream)) {
        let value: WireMessage;
        try {value = decodeLine(line);}
        catch (error) {
          if ((this.options.requestType ?? 'command') === 'command' && this.options.handler && error instanceof ProtocolError)
            await this.send({protocol_version: '1.0', type: 'response', id: null, ok: false, error: error.asData()});
          throw error;
        }
        if (value.type === 'receipt_ack') {
          const previous = this.receipts.get(key(String(value.request_type), value.id));
          if (!previous) throw new ProtocolError('UNKNOWN_RECEIPT', 'Cannot acknowledge an unknown request');
          if (previous.response) {
            if (!previous.finished) throw new ProtocolError('ACK_BEFORE_RECEIPT', 'Operation has not produced a receipt');
            previous.response = undefined; this.unacknowledged--;
          }
        } else if (value.type === 'response' || value.type === 'broker_response') {
          const pending = this.pending.get(key(value.type === 'response' ? 'command' : 'broker_request', value.id));
          if (pending) {
            if (value.ok) pending.resolve(value.result);
            else {const error = value.error as JsonObject; pending.reject(new ProtocolError(String(error.code), String(error.message), error.kind as ErrorKind));}
          } else if (value.id === null) {
            const error = value.error as JsonObject;
            throw new ProtocolError(String(error.code), String(error.message), error.kind as ErrorKind);
          }
        } else if (value.type === 'event' && this.options.eventHandler) await this.options.eventHandler(value);
        else if (value.type === (this.options.requestType ?? 'command') && this.options.handler) {
          if (this.deliveries.size >= (this.options.maxPending ?? 64)) throw new ProtocolError('PENDING_LIMIT', 'Inbound request limit reached');
          const delivery = this.dispatch(value); this.deliveries.add(delivery);
          void delivery.finally(() => this.deliveries.delete(delivery)).catch(() => {});
        } else throw new ProtocolError('INVALID_MESSAGE', 'Unexpected message direction');
      }
    } catch (error) {
      failure = error instanceof ProtocolError ? error : new ProtocolError('INVALID_FRAME', 'Peer stream failed');
      throw failure;
    } finally {
      this.closed = true;
      for (const pending of this.pending.values()) pending.reject(failure);
      this.pending.clear();
    }
  }
}

export class Context {
  private sequences = new Map<string, number>();
  private emitting: Promise<void> = Promise.resolve();
  readonly controller = new AbortController();
  private scope: Scope | undefined;
  constructor(readonly peer: Peer) {}
  get signal(): AbortSignal {return this.controller.signal;}
  bind(scope: Scope): void {
    if (this.scope && stable(this.scope) !== stable(scope))
      throw new ProtocolError('SCOPE_CONFLICT', 'A process belongs to one trial generation');
    this.scope = {...scope};
  }
  async emit(scope: Scope, event: string, data: JsonObject, source = 'benchmark'): Promise<void> {
    const write = this.emitting.then(async () => {
      const identity = stable(scope), seq = this.sequences.get(identity) ?? 0;
      await this.peer.send({protocol_version: '1.0', type: 'event', id: randomUUID(), scope, seq, source, event, data});
      this.sequences.set(identity, seq + 1);
    });
    this.emitting = write.catch(() => {}); await write;
  }
  async broker(scope: Scope, provider: string, operation: string, payload: JsonObject,
    options: {handle?: string; requestId?: string; timeout?: number} = {}): Promise<JsonValue> {
    return this.peer.request({protocol_version: '1.0', type: 'broker_request', id: options.requestId ?? randomUUID(),
      scope, provider, operation, payload, ...(options.handle ? {handle: options.handle} : {})}, options.timeout);
  }
}

export {command};
