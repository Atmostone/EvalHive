import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {RESULT_TYPES} from './connector.js';
import {Context, Peer} from './peer.js';
import {command, ProtocolError, validate, type JsonObject, type JsonValue, type Scope, type WireMessage} from './protocol.js';

export class ConnectorClient {
  readonly peer: Peer;
  private queue: WireMessage[] = [];
  private wake: (() => void) | undefined;
  private reading: Promise<void>;
  private error: ProtocolError | undefined;
  private receivedEvents = 0;
  get eventsReceived(): number {return this.receivedEvents;}
  constructor(readonly process: ChildProcessWithoutNullStreams, options: {
    broker?: (request: WireMessage) => Promise<JsonValue>; brokerReplay?: (request: WireMessage) => Promise<JsonValue>; eventBuffer?: number;
    agent?: (request: WireMessage) => Promise<JsonValue>; agentReplay?: (request: WireMessage) => Promise<JsonValue>;
    maxPending?: number; maxActive?: number; receiptLimit?: number;
  } = {}) {
    const eventBuffer = options.eventBuffer ?? 128;
    if (!Number.isInteger(eventBuffer) || eventBuffer < 1) throw new Error('Event buffer must be a positive integer');
    const reverse = async (request: WireMessage, replay = false): Promise<JsonValue> => {
      let selected;
      if (request.type === 'broker_request') selected = replay ? options.brokerReplay : options.broker;
      else if (request.method === 'agent.call') selected = replay ? options.agentReplay : options.agent;
      else throw new ProtocolError('WRONG_DIRECTION', 'Only agent calls may target the host', 'configuration');
      if (!selected) throw new ProtocolError(replay ? 'RECEIPT_UNAVAILABLE' : 'HOST_CAPABILITY_UNAVAILABLE', 'Requested host operation is unavailable', replay ? 'uncertain' : 'unsupported');
      return selected(request);
    };
    this.peer = new Peer(frame => new Promise<void>((resolve, reject) => {
      process.stdin.write(frame, error => error ? reject(error) : resolve());
    }), {...options, handler: options.agent ? request => reverse(request) : options.broker,
      replayHandler: options.agent ? request => reverse(request, true) : options.brokerReplay,
      requestTypes: options.agent ? ['command', 'broker_request'] : ['broker_request'], eventHandler: async event => {
      if (this.queue.length >= eventBuffer) throw new ProtocolError('EVENT_BUFFER_FULL', 'Event consumer fell behind');
      this.queue.push(event); this.receivedEvents++; this.wake?.();
    }});
    process.once('error', () => process.stdout.destroy(new Error('Connector failed to start')));
    process.stdin.on('error', () => {});
    this.reading = this.peer.read(process.stdout).catch(error => {
      this.error ??= error instanceof ProtocolError ? error : new ProtocolError('TRANSPORT_CLOSED', 'Connector stream failed');
    }).finally(() => this.wake?.());
  }
  static launch(argv: string[], options: {cwd?: string; env?: NodeJS.ProcessEnv;
    broker?: (request: WireMessage) => Promise<JsonValue>; brokerReplay?: (request: WireMessage) => Promise<JsonValue>; eventBuffer?: number;
    agent?: (request: WireMessage) => Promise<JsonValue>; agentReplay?: (request: WireMessage) => Promise<JsonValue>;
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
  async requestReceipt(method: string, params: JsonObject = {}, options: {requestId?: string; timeout?: number} = {}): Promise<WireMessage> {
    return await this.peer.request(command(options.requestId ?? randomUUID(), method, params), options.timeout, true) as WireMessage;
  }
  async *events(): AsyncGenerator<WireMessage> {
    while (!this.peer.closed || this.queue.length) {
      if (this.queue.length) {yield this.queue.shift()!; continue;}
      await new Promise<void>(resolve => {this.wake = resolve;}); this.wake = undefined;
    }
    if (this.error) throw this.error;
  }
  async resource(scope: Scope, provider: string, operation: string, payload: JsonObject,
    options: {handle?: string; requestId?: string; timeout?: number; durableHost?: boolean} = {}): Promise<JsonValue> {
    return new Context(this.peer).broker(scope, provider, operation, payload, options);
  }
  async close(grace = 3000, shutdown = true): Promise<void> {
    if (!Number.isFinite(grace) || grace <= 0 || grace > 86400000) throw new Error('Close grace must be between 0 and 86400000 milliseconds');
    const bounded = async (pending: Promise<unknown>): Promise<boolean> => {
      let timer: NodeJS.Timeout;
      try {return await Promise.race([pending.then(() => true), new Promise<boolean>(resolve => {timer = setTimeout(() => resolve(false), grace);})]);}
      finally {clearTimeout(timer!);}
    };
    if (shutdown && !this.peer.closed) {try {await bounded(this.request('shutdown', {}, {timeout: grace}));} catch {}}
    this.process.stdin.end();
    const exited = () => this.process.pid === undefined || this.process.exitCode !== null || this.process.signalCode !== null;
    const wait = async () => {
      if (exited()) return true;
      return await new Promise<boolean>(resolve => {
        const cleanup = (done: boolean) => {clearTimeout(timer); this.process.off('exit', onExit); this.process.off('error', onError); resolve(done);};
        const onExit = () => cleanup(true), onError = () => cleanup(exited());
        const timer = setTimeout(() => cleanup(false), grace);
        this.process.once('exit', onExit); this.process.once('error', onError);
      });
    };
    let incomplete = false;
    try {
      if (!await wait()) {this.process.kill('SIGTERM'); if (!await wait()) {this.process.kill('SIGKILL'); incomplete = !await wait();}}
      incomplete = !await bounded(this.reading) || incomplete;
    } finally {
      // An inherited pipe can outlive its parent. Detaching never implies that
      // the authoritative host has released the underlying native resource.
      this.process.stdin.destroy(); this.process.stdout.destroy(); this.process.stderr.destroy();
    }
    if (incomplete) {
      this.error = new ProtocolError('TRANSPORT_CLOSE_INCOMPLETE', 'Worker transport detached before confirmed process and stream termination', 'recording');
      this.wake?.(); throw this.error;
    }
  }
}
