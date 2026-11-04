import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import {
  ProtocolError, command, decodeLine, encodeLine, lines, validate,
  type Command, type HarnessEvent, type JsonObject, type JsonValue,
} from './protocol.js';

export class ConnectorClient {
  private pending = new Map<string, {resolve: (value: JsonValue) => void; reject: (error: Error) => void}>();
  private queue: HarnessEvent[] = [];
  private wake?: () => void;
  private closed = false;
  private closing = false;
  private streamError?: ProtocolError;
  private reading: Promise<void>;
  constructor(readonly process: ChildProcessWithoutNullStreams, private maxPending = 64, private eventBuffer = 128) {
    if (!Number.isInteger(maxPending) || maxPending < 1 || !Number.isInteger(eventBuffer) || eventBuffer < 1)
      throw new Error('Buffer limits must be positive integers');
    this.reading = this.read();
    process.once('error', () => this.fail(new ProtocolError('TRANSPORT_CLOSED', 'Connector could not start')));
    process.stdin.on('error', () => this.fail(new ProtocolError('TRANSPORT_CLOSED', 'Connector transport is closed')));
  }
  static launch(argv: string[], options: {cwd?: string; env?: NodeJS.ProcessEnv; maxPending?: number; eventBuffer?: number} = {}): ConnectorClient {
    if (!argv.length || !argv.every(x => typeof x === 'string' && x.length)) throw new Error('Entrypoint must be a nonempty argv list');
    for (const limit of [options.maxPending ?? 64, options.eventBuffer ?? 128])
      if (!Number.isInteger(limit) || limit < 1) throw new Error('Buffer limits must be positive integers');
    const child = spawn(argv[0], argv.slice(1), {cwd: options.cwd, env: options.env, stdio: ['pipe', 'pipe', 'pipe']});
    // Diagnostics stay separate from the protocol and are never accumulated by the SDK.
    child.stderr.pipe(process.stderr);
    return new ConnectorClient(child, options.maxPending, options.eventBuffer);
  }
  async request(method: Command['method'], params: JsonObject = {}, timeout = 30_000): Promise<JsonValue> {
    if (this.closed) throw new ProtocolError('TRANSPORT_CLOSED', 'Connector transport is closed');
    if (this.pending.size >= this.maxPending) throw new ProtocolError('PENDING_LIMIT', 'Too many pending requests');
    const id = randomUUID(), data = encodeLine(command(id, method, params));
    let timer: NodeJS.Timeout;
    const result = new Promise<JsonValue>((resolve, reject) => {
      timer = setTimeout(() => {this.pending.delete(id); reject(new ProtocolError('TIMEOUT', 'Connector request timed out'));}, timeout);
      this.pending.set(id, {resolve, reject});
      this.process.stdin.write(data, error => {if (error) reject(new ProtocolError('TRANSPORT_CLOSED', 'Connector transport is closed'));});
    });
    try {
      const value = await result;
      if (method === 'describe') validate('manifest', value);
      return value;
    } finally { clearTimeout(timer!); this.pending.delete(id); }
  }
  private fail(error?: ProtocolError): void {
    if (this.closed) return;
    this.closed = true; this.streamError = error;
    for (const request of this.pending.values()) request.reject(error ?? new ProtocolError('TRANSPORT_CLOSED', 'Connector disconnected'));
    this.pending.clear(); this.wake?.();
  }
  private async read(): Promise<void> {
    try {
      for await (const line of lines(this.process.stdout)) {
        const value = decodeLine(line);
        if (value.type === 'event') {
          if (this.queue.length >= this.eventBuffer) throw new ProtocolError('EVENT_BUFFER_FULL', 'Event consumer fell behind');
          this.queue.push(value); this.wake?.();
        } else if (value.type === 'response') {
          if (value.id === null && !value.ok) throw new ProtocolError(value.error.code, value.error.message);
          const pending = value.id === null ? undefined : this.pending.get(value.id);
          if (!pending) continue;
          this.pending.delete(value.id!);
          if (value.ok) pending.resolve(value.result);
          else pending.reject(new ProtocolError(value.error.code, value.error.message));
        } else throw new ProtocolError('INVALID_MESSAGE', 'Unexpected command from connector');
      }
      this.fail(this.closing ? undefined : new ProtocolError('TRANSPORT_CLOSED', 'Connector disconnected'));
    } catch (error) {
      this.fail(error instanceof ProtocolError ? error : new ProtocolError('INVALID_FRAME', 'Connector stream is invalid'));
    }
  }
  async *events(): AsyncGenerator<HarnessEvent> {
    while (!this.closed || this.queue.length) {
      if (this.queue.length) { yield this.queue.shift()!; continue; }
      await new Promise<void>(resolve => {this.wake = resolve;});
      this.wake = undefined;
    }
    if (this.streamError) throw this.streamError;
  }
  async close(grace = 3000): Promise<void> {
    this.closing = true;
    if (!this.closed) { try { await this.request('shutdown', {}, grace); } catch {} }
    this.process.stdin.end();
    const exited = () => this.process.pid === undefined || this.process.exitCode !== null || this.process.signalCode !== null;
    const wait = async () => {
      if (exited()) return true;
      return new Promise<boolean>(resolve => {
        const finish = () => {clearTimeout(timer); this.process.off('exit', finish); resolve(true);};
        const timer = setTimeout(() => {this.process.off('exit', finish); resolve(false);}, grace);
        this.process.once('exit', finish);
      });
    };
    if (!await wait()) {this.process.kill('SIGTERM'); if (!await wait()) {this.process.kill('SIGKILL'); await wait();}}
    await this.reading;
  }
}
