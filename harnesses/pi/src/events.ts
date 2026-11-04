import { ProtocolError, type JsonObject, type JsonValue, type RunContext } from '@evalhive/harness-sdk';
import { ADAPTER_VERSION, object } from './config.js';

export function json(value: unknown): JsonValue {
  return JSON.parse(JSON.stringify(value)) as JsonValue;
}

export function decimal(value: number): string {
  const text = String(value);
  if (!text.includes('e')) return text;
  const [mantissa, exponent] = text.split('e');
  const [whole, fraction = ''] = mantissa.split('.');
  const digits = whole + fraction;
  const position = whole.length + Number(exponent);
  if (position <= 0) return '0.' + '0'.repeat(-position) + digits;
  return position >= digits.length ? digits + '0'.repeat(position - digits.length) : digits.slice(0, position) + '.' + digits.slice(position);
}

export function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export class EventQueue {
  private tail: Promise<void> = Promise.resolve();
  private bytes = 0;
  error: ProtocolError | undefined;
  constructor(private context: RunContext, private sessionId: string, private abort: () => void) {}
  emit(name: string, data: JsonObject): void {
    if (this.error) return;
    const size = Buffer.byteLength(JSON.stringify(data));
    if (this.bytes + size > 8 * 1024 * 1024) {
      this.fail(new ProtocolError('EVENT_QUEUE_FULL', 'Pi event consumer fell behind'));
      return;
    }
    this.bytes += size;
    this.tail = this.tail.then(async () => {
      if (!this.error) await this.context.emit(name, data, {source: 'pi', session_id: this.sessionId});
    }).catch(() => this.fail(new ProtocolError('EVENT_ENCODING_FAILED', 'Pi event could not be delivered')))
      .finally(() => { this.bytes -= size; });
  }
  native(name: string, payload: unknown): void {
    try { this.emit('native', {name, payload: json(payload), adapter_version: ADAPTER_VERSION}); }
    catch { this.fail(new ProtocolError('NATIVE_EVENT_INVALID', 'Pi event is not JSON compatible')); }
  }
  private fail(error: ProtocolError): void { this.error ??= error; this.abort(); }
  async drain(): Promise<void> { await this.tail; if (this.error) throw this.error; }
}

export class UsageTracker {
  private index = 0;
  private current: JsonObject | undefined;
  constructor(private output: EventQueue, private model: string) {}
  get requests(): number { return this.index; }
  begin(purpose: string): void {
    this.current = {usage_id: `request-${++this.index}`, mode: 'cumulative', model: this.model,
      purpose, input_tokens: null, output_tokens: null, reasoning_tokens: null,
      cache_read_tokens: null, cache_write_tokens: null, cost: null};
    this.output.emit('usage', {...this.current});
  }
  provider(data: unknown, model: string): void {
    if (!this.current) return;
    const value = object(data);
    const usage = object(value.usage);
    if (!Object.keys(usage).length) return;
    this.current.model = model;
    for (const [wire, name] of [['prompt_tokens', 'input_tokens'], ['completion_tokens', 'output_tokens']] as const) {
      if (Object.hasOwn(usage, wire)) this.current[name] = count(usage[wire]);
    }
    const prompt = object(usage.prompt_tokens_details);
    const completion = object(usage.completion_tokens_details);
    if (Object.hasOwn(prompt, 'cached_tokens')) this.current.cache_read_tokens = count(prompt.cached_tokens);
    if (Object.hasOwn(completion, 'reasoning_tokens')) this.current.reasoning_tokens = count(completion.reasoning_tokens);
    this.output.emit('usage', {...this.current});
  }
  assistant(usage: unknown): void {
    if (!this.current) return;
    const value = object(usage);
    const cost = object(value.cost);
    if (cost.totalFromProvider === true && typeof cost.total === 'number' && Number.isFinite(cost.total) && cost.total >= 0)
      this.current.cost = {amount: decimal(cost.total), currency: 'USD', source: 'provider'};
    this.output.emit('usage', {...this.current});
  }
  auxiliary(identity: string, usage: unknown, model: string): void {
    const value = object(usage);
    const input = [value.input, value.cacheRead, value.cacheWrite].map(count);
    const total = input.every((part) => part !== null) ? input.reduce<number>((sum, part) => sum + (part ?? 0), 0) : null;
    const positive = (field: unknown) => typeof field === 'number' && field > 0 ? count(field) : null;
    const cost = object(value.cost);
    this.output.emit('usage', {usage_id: identity, mode: 'cumulative', model, purpose: 'compaction',
      input_tokens: total && total > 0 ? total : null, output_tokens: positive(value.output),
      reasoning_tokens: count(value.reasoning), cache_read_tokens: positive(value.cacheRead),
      cache_write_tokens: positive(value.cacheWrite),
      cost: cost.totalFromProvider === true && typeof cost.total === 'number' && Number.isFinite(cost.total) && cost.total >= 0 ?
        {amount: decimal(cost.total), currency: 'USD', source: 'provider'} : null});
  }
}
