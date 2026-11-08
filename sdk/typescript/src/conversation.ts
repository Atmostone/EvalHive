import {createHash} from 'node:crypto';
import {ProtocolError, validate, type JsonObject, type JsonValue} from './protocol.js';
import type {RunContext} from './connector.js';

export const CONVERSATION_COMMANDS: Record<string, string> = {
  'turn.submit': 'conversation_input', 'tool.respond': 'conversation_tool_response', 'session.close': 'conversation_close',
};
export const CONVERSATION_EVENTS = new Set(['turn.started', 'turn.output', 'turn.completed', 'session.waiting_input', 'tool.request']);
function stable(value: JsonValue): string {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + stable(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
const signature = (value: JsonValue) => createHash('sha256').update(stable(value)).digest('hex');
export class Conversation {
  readonly config: JsonObject;
  readonly mode: string;
  revision = 0;
  state = 'waiting_input';
  closeReason: string | undefined;
  private current: JsonObject | undefined;
  private queue: (JsonObject | null)[] = [];
  private wake: (() => void) | undefined;
  private receipts = new Map<string, {hash: string; receipt: JsonObject}>();
  private turns = new Set<string>();
  private batches = new Set<string>();
  private toolCount = 0;
  private pending: {batchId: string; calls: string[]; results: Map<string, JsonObject>;
    resolve: (results: JsonObject[]) => void} | undefined;
  private locked: Promise<void> = Promise.resolve();
  constructor(private context: RunContext) {
    this.config = context.spec.interaction as JsonObject;
    this.mode = String(this.config.mode);
  }
  private async atomic<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.locked.then(operation);
    this.locked = result.then(() => {}, () => {}); return result;
  }
  private emit(event: string, data: JsonObject): Promise<void> {return this.context.lifecycle(event, data);}
  async ready(): Promise<void> {
    await this.atomic(async () => {
      if (this.state === 'waiting_input') await this.emit('session.waiting_input', {revision: this.revision});
    });
  }
  private async wait<T>(promise: Promise<T>): Promise<T> {
    if (this.context.signal.aborted) throw new ProtocolError('CANCELLED', 'Conversation cancelled');
    let stop!: () => void;
    const cancelled = new Promise<T>((_, reject) => {
      stop = () => reject(new ProtocolError('CANCELLED', 'Conversation cancelled'));
      this.context.signal.addEventListener('abort', stop, {once: true});
    });
    try {return await Promise.race([promise, cancelled]);}
    finally {this.context.signal.removeEventListener('abort', stop);}
  }
  async nextInput(): Promise<JsonObject | null> {
    while (!this.queue.length) {
      if (this.state === 'closed') return null;
      await this.wait(new Promise<void>(resolve => {this.wake = resolve;}));
      this.wake = undefined;
    }
    const value = this.queue.shift()!;
    if (value === null) return null;
    return this.atomic(async () => {
      this.current = value; this.state = 'generating';
      await this.emit('turn.started', this.turn()); return value;
    });
  }
  private turn(): JsonObject {
    if (!this.current) throw new ProtocolError('NO_ACTIVE_TURN', 'Conversation has no active input');
    return {turn_id: this.current.turn_id, input_id: this.current.input_id, revision: this.revision};
  }
  async control(name: string, payload: JsonObject): Promise<JsonObject> {
    validate(CONVERSATION_COMMANDS[name], payload);
    return this.atomic(async () => {
      const id = String(payload.input_id), hash = signature({command: name, payload}), previous = this.receipts.get(id);
      if (previous) {
        if (previous.hash !== hash) throw new ProtocolError('INPUT_CONFLICT', 'Input identity has different content');
        return previous.receipt;
      }
      if (name !== 'session.close' && this.receipts.size >= Number(this.config.max_inputs ?? 1024)) throw new ProtocolError('INPUT_LIMIT', 'Conversation input ledger is full');
      if (payload.expected_revision !== this.revision) throw new ProtocolError('REVISION_CONFLICT', 'Conversation revision changed');
      let receipt: JsonObject;
      if (name === 'turn.submit') {
        if (this.state !== 'waiting_input') throw new ProtocolError('INPUT_BUSY', 'Conversation is not waiting for input');
        if (this.turns.has(String(payload.turn_id))) throw new ProtocolError('TURN_CONFLICT', 'Conversation turn identity was already used');
        this.turns.add(String(payload.turn_id)); this.revision++; this.state = 'queued';
        this.queue.push(payload); this.wake?.();
        receipt = {accepted: true, revision: this.revision, input_id: id};
      } else if (name === 'tool.respond') receipt = this.respond(payload);
      else {
        if (this.state !== 'waiting_input') throw new ProtocolError('INPUT_BUSY', 'Close requires an idle conversation; abort stops active work');
        this.revision++; this.state = 'closed'; this.closeReason = String(payload.reason);
        this.queue.push(null); this.wake?.();
        await this.emit('session.end', {revision: this.revision, reason: this.closeReason});
        receipt = {accepted: true, revision: this.revision, input_id: id};
      }
      this.receipts.set(id, {hash, receipt}); return receipt;
    });
  }
  private respond(payload: JsonObject): JsonObject {
    const pending = this.pending;
    if (this.state !== 'waiting_tool' || !pending) throw new ProtocolError('NO_PENDING_TOOLS', 'Conversation is not waiting for tools');
    if (payload.turn_id !== this.current!.turn_id || payload.batch_id !== pending.batchId)
      throw new ProtocolError('TOOL_BATCH_CONFLICT', 'Tool batch does not match the active step');
    const results = payload.results as JsonObject[], ids = results.map(item => String(item.call_id));
    if (new Set(ids).size !== ids.length || ids.some(id => !pending.calls.includes(id)))
      throw new ProtocolError('UNKNOWN_TOOL_CALL', 'Tool result identities do not match the active batch');
    for (const result of results) {
      const previous = pending.results.get(String(result.call_id));
      if (previous && signature(previous) !== signature(result)) throw new ProtocolError('TOOL_RESULT_CONFLICT', 'A tool result was already fixed');
    }
    for (const result of results) pending.results.set(String(result.call_id), result);
    if (pending.results.size === pending.calls.length) {
      this.state = 'generating'; pending.resolve(pending.calls.map(id => pending.results.get(id)!));
    }
    return {accepted: true, revision: this.revision, input_id: payload.input_id};
  }
  async requestTools(messageId: string, batchId: string, calls: JsonObject[], content = ''): Promise<JsonObject[]> {
    if (this.mode !== 'external_steps') throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'External tool execution was not selected');
    let output!: Promise<JsonObject[]>;
    await this.atomic(async () => {
      if (this.state !== 'generating') throw new ProtocolError('INPUT_BUSY', 'Conversation already has pending work');
      const data = {...this.turn(), message_id: messageId, batch_id: batchId, calls};
      validate('conversation_tool_request', data);
      const ids = calls.map(call => String(call.call_id));
      if (new Set(ids).size !== ids.length || this.batches.has(batchId)) throw new ProtocolError('TOOL_BATCH_CONFLICT', 'Tool batch identity was already used');
      if (this.toolCount + calls.length > Number(this.config.max_tool_calls ?? 4096)) throw new ProtocolError('TOOL_LIMIT', 'Conversation tool ledger is full');
      this.toolCount += calls.length; this.batches.add(batchId);
      output = new Promise<JsonObject[]>(resolve => {
        this.pending = {batchId, calls: ids, results: new Map(), resolve};
      });
      this.state = 'waiting_tool';
      await this.emit('turn.output', {...this.turn(), message_id: messageId, output: {role: 'assistant', content, tool_calls: calls}});
      await this.emit('tool.request', data);
    });
    try {return await this.wait(output);} finally {this.pending = undefined;}
  }
  async completeTurn(messageId: string, content: string): Promise<void> {
    await this.atomic(async () => {
      if (this.state !== 'generating' || this.pending) throw new ProtocolError('INPUT_BUSY', 'A turn cannot finish with pending work');
      const turn = this.turn();
      await this.emit('turn.output', {...turn, message_id: messageId, output: {role: 'assistant', content}});
      await this.emit('turn.completed', turn);
      this.current = undefined; this.state = 'waiting_input';
      await this.emit('session.waiting_input', {revision: this.revision});
    });
  }
}
