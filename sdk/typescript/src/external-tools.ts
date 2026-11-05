import {createHash} from 'node:crypto';
import {ProtocolError, type JsonObject, type JsonValue, type RunSpec} from './protocol.js';
import type {Conversation} from './conversation.js';

const stable = (value: JsonValue): string => Array.isArray(value) ? '[' + value.map(stable).join(',') + ']' :
  value && typeof value === 'object' ? '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + stable(value[k])).join(',') + '}' : JSON.stringify(value);
export const contentHash = (value: JsonValue): string => createHash('sha256').update(stable(value)).digest('hex');

/** Check causal history before any native process or model request is started. */
export function conversationHistory(spec: RunSpec): JsonObject[] {
  const history = (spec.interaction as JsonObject | undefined)?.initial_history as JsonObject[] | undefined ?? [];
  const pending = new Set<string>(), seen = new Set<string>();
  for (const message of history) {
    if (message.role === 'tool') {
      if (!pending.delete(String(message.call_id))) throw new ProtocolError('INVALID_HISTORY', 'History contains an unmatched tool result');
    } else {
      if (pending.size) throw new ProtocolError('INVALID_HISTORY', 'History contains an unfinished tool batch');
      for (const call of (message.tool_calls as JsonObject[] | undefined) ?? []) {
        const id = String(call.call_id);
        if (seen.has(id)) throw new ProtocolError('INVALID_HISTORY', 'History reuses a tool call identity');
        seen.add(id); pending.add(id);
      }
    }
  }
  if (pending.size) throw new ProtocolError('INVALID_HISTORY', 'History ends with unfinished tools');
  return history;
}

/** Seeded tool names use the same declared mapping as newly generated calls. */
export function mapHistoryTools(history: JsonObject[], mapping: JsonObject[]): JsonObject[] {
  return history.map(message => ({...message, ...(message.tool_calls ? {
    tool_calls: (message.tool_calls as JsonObject[]).map(call => {
      const candidates = mapping.filter(tool => [tool.native_name, tool.canonical_name, tool.exposed_name].includes(call.name));
      if (candidates.length > 1) throw new ProtocolError('INVALID_HISTORY', 'History tool name is ambiguous');
      return {...call, name: candidates[0]?.exposed_name ?? call.name};
    }),
  } : {})}));
}

/** Correlate native execution with a complete driver-owned batch, never by FIFO. */
export class ExternalToolGate {
  private calls = new Map<string, {name: string; hash: string; result: Promise<JsonObject>}>();
  private announced = new Set<() => void>();
  constructor(private conversation: Conversation, private signal: AbortSignal) {}
  announce(messageId: string, batchId: string, calls: JsonObject[], content = ''): Promise<JsonObject[]> {
    if (calls.some(call => this.calls.has(String(call.call_id))))
      throw new ProtocolError('TOOL_CALL_CONFLICT', 'Native tool call identity was already used');
    const results = this.conversation.requestTools(messageId, batchId, calls, content);
    results.catch(() => {});
    for (const call of calls) {
      const result = results.then(values => values.find(value => value.call_id === call.call_id)!);
      result.catch(() => {});
      this.calls.set(String(call.call_id), {name: String(call.name), hash: contentHash(call.arguments), result});
    }
    for (const wake of this.announced) wake();
    return results;
  }
  async execute(callId: string, name: string, args: JsonObject): Promise<JsonObject> {
    if (this.announced.size >= 128) throw new ProtocolError('TOOL_LIMIT', 'Too many native tools await a batch');
    // Native streaming execution may reach a tool before the final batch frame.
    if (!this.calls.has(callId)) await new Promise<void>((resolve, reject) => {
      const done = (error?: Error) => {
        clearTimeout(timer); this.announced.delete(wake); this.signal.removeEventListener('abort', abort);
        error ? reject(error) : resolve();
      };
      const abort = () => done(new ProtocolError('CANCELLED', 'External tools cancelled'));
      const wake = () => {if (this.calls.has(callId)) done();};
      const timer = setTimeout(() => done(new ProtocolError('TOOL_BATCH_MISSING', 'Native tool has no completed batch')), 10000);
      this.announced.add(wake); this.signal.addEventListener('abort', abort, {once: true});
      if (this.signal.aborted) abort();
    });
    const call = this.calls.get(callId)!;
    if (call.name !== name || call.hash !== contentHash(args))
      throw new ProtocolError('TOOL_CALL_CONFLICT', 'Native tool differs from the announced call');
    return call.result;
  }
}
