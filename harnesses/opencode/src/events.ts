import { ProtocolError, type JsonObject, type RunContext } from '@evalhive/harness-sdk';
import { ADAPTER_VERSION, object } from './config.js';
import { createHash } from 'node:crypto';

export class EventWriter {
  private tail: Promise<void> = Promise.resolve();
  private bytes = 0;
  error: ProtocolError | undefined;
  constructor(
    private context: RunContext,
    private stop: () => void,
  ) {}
  emit(name: string, data: JsonObject, session?: string, parent?: string): void {
    if (this.error) return;
    const size = Buffer.byteLength(JSON.stringify(data));
    if (size + this.bytes > 8 * 1024 * 1024) {
      this.fail('EVENT_QUEUE_FULL', 'OpenCode event consumer fell behind');
      return;
    }
    this.bytes += size;
    this.tail = this.tail
      .then(async () => {
        if (!this.error)
          await this.context.emit(name, data, {
            source: 'opencode',
            ...(session ? { session_id: session } : {}),
            ...(parent ? { parent_session_id: parent } : {}),
          });
      })
      .catch(() => this.fail('EVENT_ENCODING_FAILED', 'OpenCode event could not be delivered'))
      .finally(() => {
        this.bytes -= size;
      });
  }
  native(name: string, value: unknown, session?: string, parent?: string): void {
    try {
      this.emit(
        'native',
        { name, payload: JSON.parse(JSON.stringify(value)), adapter_version: ADAPTER_VERSION },
        session,
        parent,
      );
    } catch {
      this.fail('NATIVE_EVENT_INVALID', 'OpenCode event is not JSON compatible');
    }
  }
  fail(code: string, message: string): void {
    this.error ??= new ProtocolError(code, message);
    this.stop();
  }
  async drain(): Promise<void> {
    await this.tail;
    if (this.error) throw this.error;
  }
}

/** Native durable IDs fence SSE replay; normalized records use native call IDs. */
export class EventTranslator {
  readonly imported = new Set<string>();
  readonly seen = new Set<string>();
  readonly parents = new Map<string, string>();
  readonly final = new Map<string, { finish?: string; error?: unknown }>();
  private messages = new Map<string, Record<string, unknown>>();
  private parts = new Map<string, Record<string, unknown>>();
  private hashes = new Map<string, string>();
  private started = new Set<string>();
  private ended = new Set<string>();
  private messageEnds = new Map<string, string>();
  private parentCalls = new Map<string, Map<string, {session: string; call: string; start?: number; end?: number}>>();
  constructor(
    readonly output: EventWriter,
    readonly aliases: Record<string, string[]> = {},
    readonly aliasPrefixes: Record<string, string> = {},
  ) {}
  snapshot(entries: unknown[]): void {
    for (const entry of entries) {
      const value = object(entry),
        info = object(value.info);
      if (this.imported.has(String(info.id))) continue;
      this.messages.set(`${info.sessionID}/${info.id}`, info);
    }
    for (const entry of entries) {
      const value = object(entry),
        info = object(value.info);
      if (this.imported.has(String(info.id))) continue;
      const fingerprint = createHash('sha256').update(JSON.stringify(value)).digest('hex');
      const key = `snapshot/${info.sessionID}/${info.id}`;
      if (this.hashes.get(key) !== fingerprint) {
        this.output.native(
          'message.snapshot',
          value,
          String(info.sessionID),
          this.parents.get(String(info.sessionID)),
        );
        this.hashes.set(key, fingerprint);
      }
      for (const part of Array.isArray(value.parts) ? value.parts : []) this.part(object(part));
    }
  }
  private part(part: Record<string, unknown>): void {
    if (this.imported.has(String(part.messageID))) return;
    const info = this.messages.get(`${part.sessionID}/${part.messageID}`);
    this.parts.set(`${part.sessionID}/${part.id}`, part);
    if (!info) return;
    const fingerprint = createHash('sha256')
      .update(
        JSON.stringify({
          part,
          ...(['text', 'reasoning'].includes(String(part.type))
            ? { completed: object(info.time).completed }
            : {}),
        }),
      )
      .digest('hex');
    const key = `part/${part.sessionID}/${part.id}`;
    if (this.hashes.get(key) === fingerprint) return;
    this.hashes.set(key, fingerprint);
    const session = String(part.sessionID),
      parent = this.parents.get(session);
    if (part.type === 'text' || part.type === 'reasoning') {
      if (
        info.role === 'user' ||
        object(part.time).end !== undefined ||
        object(info.time).completed !== undefined
      ) {
        const data = {
          message_id: String(part.id),
          role: String(info.role),
          channel: part.type === 'reasoning' ? 'reasoning' : 'text',
          content: String(part.text ?? ''),
        };
        const hash = createHash('sha256').update(JSON.stringify(data)).digest('hex');
        if (this.messageEnds.get(key) !== hash) {
          this.messageEnds.set(key, hash);
          this.output.emit('message.end', data, session, parent);
        }
      }
    } else if (part.type === 'tool') {
      const state = object(part.state),
        identity = `${session}/${part.callID}`;
      const child = object(state.metadata).sessionId;
      if (part.tool === 'task' && typeof child === 'string') {
        const calls = this.parentCalls.get(child) ?? new Map();
        const time = object(state.time);
        calls.set(identity, {session, call: String(part.callID),
          ...(typeof time.start === 'number' ? {start: time.start} : {}),
          ...(typeof time.end === 'number' ? {end: time.end} : {})});
        this.parentCalls.set(child, calls);
      }
      const start = object(state.time).start;
      const candidates = [...(this.parentCalls.get(session)?.values() ?? [])].filter(call =>
        typeof start !== 'number' || ((call.start === undefined || start >= call.start) &&
          (call.end === undefined || start <= call.end)));
      const parentCall = candidates.length === 1 ? candidates[0] : undefined;
      const lineage: JsonObject = parentCall && parentCall.session === parent ? {parent_call_id: parentCall.call} : {};
      const namespace = Object.keys(this.aliasPrefixes).find(prefix => String(part.tool).startsWith(prefix + '_'));
      if (namespace) {
        const original = String(part.tool).slice(namespace.length + 1);
        this.aliases[String(part.tool)] = [...new Set([...(this.aliases[String(part.tool)] ?? []),
          this.aliasPrefixes[namespace] + original, `mcp__${namespace}__${original}`])];
      }
      if (state.status === 'pending') return;
      if (!this.started.has(identity)) {
        const args = {...object(state.input)} as JsonObject;
        delete args.__evalhive_native_call_id;
        this.started.add(identity);
        this.output.emit(
          'tool.start',
          {
            call_id: String(part.callID),
            ...lineage,
            name: String(part.tool),
            arguments: args,
            message_id: String(part.messageID),
            ...(this.aliases[String(part.tool)]?.length ? { aliases: this.aliases[String(part.tool)] } : {}),
          },
          session,
          parent,
        );
      }
      if (state.status === 'completed' || state.status === 'error') {
        const time = object(state.time);
        // Native compaction can replace old outputs in REST snapshots. Keep
        // the original terminal evidence; a pruned-only result remains missing.
        if (this.ended.has(identity) || typeof time.compacted === 'number') return;
        this.ended.add(identity);
        this.output.emit(
          'tool.end',
          {
            call_id: String(part.callID),
            ...lineage,
            status: state.status === 'error' ? 'error' : 'completed',
            result: (state.output ?? state.error ?? state.metadata ?? '') as JsonObject,
            ...(typeof time.start === 'number' && typeof time.end === 'number'
              ? { duration_ms: Math.max(0, time.end - time.start) }
              : {}),
          },
          session,
          parent,
        );
      }
    }
  }
  event(input: unknown): void {
    const event = object(input);
    if (typeof event.id !== 'string' || typeof event.type !== 'string')
      throw new ProtocolError('INVALID_NATIVE_EVENT', 'OpenCode event has no stable identity');
    if (this.seen.has(event.id)) return;
    if (this.seen.size >= 100000)
      throw new ProtocolError('NATIVE_EVENT_LIMIT', 'OpenCode event identity limit reached');
    this.seen.add(event.id);
    const data = object(event.data ?? event.properties);
    const session = typeof data.sessionID === 'string' ? data.sessionID : undefined;
    const info = object(data.info);
    if (session && event.type === 'session.created' && typeof info.parentID === 'string')
      this.parents.set(session, info.parentID);
    const parent = session ? this.parents.get(session) : undefined;
    this.output.native(event.type, event, session, parent);
    if (!session) return;
    if (event.type === 'session.error') this.final.set(session, { error: data.error });
    else if (event.type === 'message.updated') {
      this.messages.set(`${info.sessionID}/${info.id}`, info);
      for (const part of this.parts.values()) if (part.messageID === info.id) this.part(part);
    } else if (event.type === 'message.part.updated') this.part(object(data.part));
    else if (event.type === 'message.part.delta') {
      const part = this.parts.get(`${session}/${data.partID}`);
      const message = this.messages.get(`${session}/${data.messageID}`);
      if (part && message && (part.type === 'text' || part.type === 'reasoning'))
        this.output.emit(
          'message.delta',
          {
            message_id: String(data.partID),
            role: String(message.role),
            channel: part.type === 'reasoning' ? 'reasoning' : 'text',
            delta: String(data.delta ?? ''),
          },
          session,
          parent,
        );
    }
  }
}
