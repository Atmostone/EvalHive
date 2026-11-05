import { readFileSync } from 'node:fs';
import { Ajv2020 } from 'ajv/dist/2020.js';
import type { ValidateFunction } from 'ajv';
import { resources } from './resources.js';

export const PROTOCOL_VERSION = '1.0' as const;
export const MAX_LINE_BYTES = 1024 * 1024;
export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export interface JsonObject { [key: string]: JsonValue }
export type Manifest = JsonObject & {
  protocol_version: '1.0'; id: string; version: string; display_name: string;
  entrypoint: string[]; image: string; config_schema: JsonObject;
  capabilities: Record<string, 'native' | 'emulated' | 'unsupported'>;
  commands?: Record<string, {input_schema: JsonObject; title?: string}>;
}
export type RunSpec = JsonObject & {
  protocol_version: '1.0'; task_id: string; attempt_id: string; workspace_id: string;
  task: {prompt: string; title?: string}; cwd: string; config: JsonObject;
  required_capabilities?: string[];
}
export type RunResult = JsonObject & {
  status: 'success' | 'failed' | 'cancelled'; summary: string; error?: ErrorData;
}
export interface ErrorData extends JsonObject { code: string; message: string }
export type NormalizedFailure = JsonObject & {
  kind: 'model_error' | 'limit' | 'timeout' | 'harness_error' | 'recording_error' | 'agent_error';
  status_code?: number | null;
  exception?: string | null;
}
export interface Command extends JsonObject {
  protocol_version: '1.0'; type: 'command'; id: string;
  method: 'describe' | 'validate' | 'start' | 'control' | 'shutdown'; params: JsonObject;
}
export type Response = {protocol_version: '1.0'; type: 'response'; id: string; ok: true; result: JsonValue}
  | {protocol_version: '1.0'; type: 'response'; id: string | null; ok: false; error: ErrorData};
export type HarnessEvent = JsonObject & {
  protocol_version: '1.0'; type: 'event'; event_id: string; attempt_id: string;
  seq: number; source: string; event: string; data: JsonObject;
  session_id?: string; parent_session_id?: string;
  observed_at?: string;
}
export type WireMessage = Command | Response | HarnessEvent;

export class ProtocolError extends Error {
  constructor(public readonly code: string, message: string) { super(message); }
  asData(): ErrorData { return {code: this.code, message: this.message}; }
}

const schema = JSON.parse(readFileSync(new URL('./schemas/contract.json', import.meta.url), 'utf8'));
// Runtime validation has one source of truth, shared with the Python wheel.
const ajv = new Ajv2020({strict: false, validateFormats: false});
ajv.addSchema(schema);
const validators = new Map<string, ValidateFunction>();

function jsonValue(value: unknown, seen = new Set<object>(), depth = 0): void {
  if (depth > 64) throw new ProtocolError('INVALID_MESSAGE', 'JSON nesting exceeds limit');
  if (typeof value === 'string') {
    if (!value.isWellFormed()) throw new ProtocolError('INVALID_MESSAGE', 'String is not valid Unicode');
    return;
  }
  if (value === null || typeof value === 'boolean') return;
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || Math.abs(value) > Number.MAX_SAFE_INTEGER)
      throw new ProtocolError('INVALID_MESSAGE', 'Number is not finite or safely representable');
    return;
  }
  if (typeof value !== 'object' || value === null || seen.has(value))
    throw new ProtocolError('INVALID_MESSAGE', 'Value is not JSON-compatible');
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null)
    throw new ProtocolError('INVALID_MESSAGE', 'Value is not JSON-compatible');
  seen.add(value);
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) jsonValue(value[i], seen, depth + 1);
  } else {
    for (const [key, item] of Object.entries(value)) {
      jsonValue(key, seen, depth + 1); jsonValue(item, seen, depth + 1);
    }
  }
  seen.delete(value);
}

export function validate<T = JsonObject>(kind: string, value: unknown): T {
  jsonValue(value);
  const root = value as JsonObject | null;
  const params = root && root.type === 'command' && root.params && typeof root.params === 'object' && !Array.isArray(root.params) ? root.params as JsonObject : undefined;
  const spec = params?.run ?? root;
  if (spec && typeof spec === 'object' && !Array.isArray(spec) && spec.interaction && typeof spec.interaction === 'object' &&
      !Array.isArray(spec.interaction) && spec.interaction.version !== '1.0')
    throw new ProtocolError('UNSUPPORTED_INTERACTION_VERSION', 'Unsupported interaction protocol version');
  if (value && typeof value === 'object' && 'protocol_version' in value && value.protocol_version !== PROTOCOL_VERSION)
    throw new ProtocolError('UNSUPPORTED_PROTOCOL', 'Unsupported protocol version');
  if (!(kind in schema.$defs)) throw new Error(`Unknown contract type: ${kind}`);
  let check = validators.get(kind);
  if (!check) {
    check = ajv.getSchema(`${schema.$id}#/$defs/${kind}`)!;
    validators.set(kind, check);
  }
  if (!check(value)) throw new ProtocolError('INVALID_MESSAGE', `Invalid ${kind} shape`);
  return value as T;
}

export function validateRun(manifest: Manifest, run: RunSpec): RunSpec {
  validate('manifest', manifest); validate('run_spec', run);
  resources(run);
  const mode = (run.interaction as JsonObject | undefined)?.mode ?? 'single_turn';
  for (const capability of [...(mode !== 'single_turn' ? ['conversation'] : []), ...(mode === 'external_steps' ? ['external_tool_execution'] : [])])
    if (!['native', 'emulated'].includes(manifest.capabilities[capability] ?? 'unsupported'))
      throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Requested interaction mode is unavailable');
  if (((run.interaction as JsonObject | undefined)?.initial_history as JsonValue[] | undefined)?.length &&
      !['native', 'emulated'].includes(manifest.capabilities.conversation_history ?? 'unsupported'))
    throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Native history initialization is unavailable');
  let valid: boolean;
  try {
    if (manifest.config_schema.$schema !== undefined && manifest.config_schema.$schema !== 'https://json-schema.org/draft/2020-12/schema')
      throw new Error('Unsupported schema dialect');
    // Separate instance prevents a connector schema from replacing protocol IDs.
    const configAjv = new Ajv2020({strict: false, validateFormats: false});
    valid = Boolean(configAjv.compile(manifest.config_schema)(run.config));
  } catch {
    throw new ProtocolError('INVALID_CONFIG', 'Invalid connector configuration schema');
  }
  if (!valid) throw new ProtocolError('INVALID_CONFIG', 'Configuration does not match connector schema');
  for (const name of run.required_capabilities ?? []) {
    if (!['native', 'emulated'].includes(manifest.capabilities[name] ?? 'unsupported'))
      throw new ProtocolError('UNSUPPORTED_CAPABILITY', `Unavailable capability: ${name}`);
  }
  return run;
}

export function command(id: string, method: Command['method'], params: JsonObject = {}): Command {
  return validate('command', {protocol_version: PROTOCOL_VERSION, type: 'command', id, method, params});
}
export function response(id: string, result: JsonValue): Response {
  return validate('response', {protocol_version: PROTOCOL_VERSION, type: 'response', id, ok: true, result});
}
export function errorResponse(id: string | null, error: ProtocolError): Response {
  return validate('response', {protocol_version: PROTOCOL_VERSION, type: 'response', id, ok: false, error: error.asData()});
}
export function encodeLine(value: WireMessage): Buffer {
  validate('wire_message', value);
  const data = Buffer.from(JSON.stringify(value), 'utf8');
  if (data.length > MAX_LINE_BYTES) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
  return Buffer.concat([data, Buffer.from('\n')]);
}
export function decodeLine(line: Uint8Array): WireMessage {
  if (line.length > MAX_LINE_BYTES + 1) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
  if (line.at(-1) !== 10) throw new ProtocolError('INVALID_FRAME', 'Protocol record must end with LF');
  let value: unknown;
  try { value = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(line.slice(0, -1))); }
  catch { throw new ProtocolError('INVALID_FRAME', 'Invalid JSON protocol record'); }
  return validate('wire_message', value);
}

/** Split ONLY on LF; U+2028/U+2029 inside text are not framing boundaries. */
export async function* lines(stream: AsyncIterable<Uint8Array>): AsyncGenerator<Buffer> {
  let buffer = Buffer.alloc(0);
  for await (const chunk of stream) {
    buffer = Buffer.concat([buffer, chunk]);
    let pos: number;
    while ((pos = buffer.indexOf(10)) !== -1) {
      yield buffer.subarray(0, pos + 1);
      buffer = buffer.subarray(pos + 1);
    }
    if (buffer.length > MAX_LINE_BYTES) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
  }
  if (buffer.length) throw new ProtocolError('INVALID_FRAME', 'Protocol record must end with LF');
}
