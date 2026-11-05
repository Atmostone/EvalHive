import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {once} from 'node:events';
import {RESULT_TYPES} from './connector.js';
import {Context, Peer} from './peer.js';
import {command, ProtocolError, validate, type JsonObject, type JsonValue, type Scope, type WireMessage} from './protocol.js';

export class ConnectorClient {
  readonly peer: Peer;
  private queue: WireMessage[] = [];
  private wake: (() => void) | undefined;
  private reading: Promise<void>;
  private error: ProtocolError | undefined;
  constructor(readonly process: ChildProcessWithoutNullStreams, options: {
    broker?: (request: WireMessage) => Promise<JsonValue>; eventBuffer?: number;
    maxPending?: number; maxActive?: number; receiptLimit?: number;
  } = {}) {
    const eventBuffer = options.eventBuffer ?? 128;
    if (!Number.isInteger(eventBuffer) || eventBuffer < 1) throw new Error('Event buffer must be a positive integer');
    this.peer = new Peer(frame => new Promise<void>((resolve, reject) => {
      process.stdin.write(frame, error => error ? reject(error) : resolve());
    }), {...options, handler: options.broker, requestType: 'broker_request', eventHandler: async event => {
      if (this.queue.length >= eventBuffer) throw new ProtocolError('EVENT_BUFFER_FULL', 'Event consumer fell behind');
      this.queue.push(event); this.wake?.();
    }});
    process.once('error', () => process.stdout.destroy(new Error('Connector failed to start')));
    process.stdin.on('error', () => {});
    this.reading = this.peer.read(process.stdout).catch(error => {
      this.error = error instanceof ProtocolError ? error : new ProtocolError('TRANSPORT_CLOSED', 'Connector stream failed');
    }).finally(() => this.wake?.());
  }
  static launch(argv: string[], options: {cwd?: string; env?: NodeJS.ProcessEnv;
    broker?: (request: WireMessage) => Promise<JsonValue>; eventBuffer?: number;
    maxPending?: number; maxActive?: number; receiptLimit?: number} = {}): ConnectorClient {
    if (!argv.length || !argv.every(value => typeof value === 'string' && value.length && !value.includes('\0')))
      throw new Error('Entrypoint must be a nonempty argv list');
    for (const n of [options.eventBuffer ?? 128, options.maxPending ?? 64, options.maxActive ?? 16, options.receiptLimit ?? 256])
      if (!Number.isInteger(n) || n < 1) throw new Error('RPC limits must be positive integers');
    const child = spawn(argv[0], argv.slice(1), {cwd: options.cwd, env: options.env, stdio: ['pipe', 'pipe', 'pipe']});
    child.stderr.pipe(process.stderr);
    return new ConnectorClient(child, options);
  }
  async request(method: string, params: JsonObject = {}, options: {requestId?: string; timeout?: number} = {}): Promise<JsonValue> {
    const result = await this.peer.request(command(options.requestId ?? randomUUID(), method, params), options.timeout);
    if (RESULT_TYPES[method]) validate(RESULT_TYPES[method], result);
    return result;
  }
  async *events(): AsyncGenerator<WireMessage> {
    while (!this.peer.closed || this.queue.length) {
      if (this.queue.length) {yield this.queue.shift()!; continue;}
      await new Promise<void>(resolve => {this.wake = resolve;}); this.wake = undefined;
    }
    if (this.error) throw this.error;
  }
  async resource(scope: Scope, provider: string, operation: string, payload: JsonObject,
    options: {handle?: string; requestId?: string; timeout?: number} = {}): Promise<JsonValue> {
    return new Context(this.peer).broker(scope, provider, operation, payload, options);
  }
  async close(grace = 3000): Promise<void> {
    if (!this.peer.closed) {try {await this.request('shutdown', {}, {timeout: grace});} catch {}}
    this.process.stdin.end();
    const exited = () => this.process.pid === undefined || this.process.exitCode !== null || this.process.signalCode !== null;
    const wait = async () => {
      if (exited()) return true;
      let timer: NodeJS.Timeout;
      const result = await Promise.race([once(this.process, 'exit').then(() => true),
        new Promise<boolean>(resolve => {timer = setTimeout(() => resolve(false), grace);})]);
      clearTimeout(timer!); return result;
    };
    if (!await wait()) {this.process.kill('SIGTERM'); if (!await wait()) {this.process.kill('SIGKILL'); await wait();}}
    await this.reading;
  }
}
