import {readFileSync} from 'node:fs';
import {Ajv2020} from 'ajv/dist/2020.js';
import type {ValidateFunction} from 'ajv';

export const PROTOCOL_VERSION = '1.0' as const;
export const MAX_LINE_BYTES = 1024 * 1024;
export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export interface JsonObject { [key: string]: JsonValue }
export type Scope = JsonObject & {workspace_id: string; trial_id: string; generation: number};
export type Manifest = JsonObject & {
  protocol_version: '1.0'; kind: 'benchmark' | 'environment_provider';
  id: string; version: string; display_name: string; entrypoint: string[]; image: string;
  config_schema: JsonObject; capabilities: Record<string, string>; requirements: JsonObject;
};
export type TrialSpec = JsonObject & {protocol_version: '1.0'; scope: Scope;
  case: JsonObject; config: JsonObject; models: JsonObject; credential_refs: JsonObject};
export type EnvironmentPlan = JsonObject & {resources: JsonObject[]; agent: JsonObject;
  outputs: JsonObject[]; requirements: JsonObject; grading_mode: string};
export type EvaluationResult = JsonObject & {
  status: 'completed' | 'error' | 'unavailable' | 'cancelled'; success: boolean | null;
  metrics: JsonObject[]; native: JsonObject; artifacts: JsonObject[]; completeness: JsonObject;
};
export type WireMessage = JsonObject & {type: string; id: string | null};
export type ErrorKind = 'configuration' | 'unsupported' | 'preparation' | 'execution' |
  'grading' | 'recording' | 'cleanup' | 'transport' | 'uncertain';

export class ProtocolError extends Error {
  constructor(public readonly code: string, message: string, public readonly kind: ErrorKind = 'transport') {super(message);}
  asData(): JsonObject {return {code: this.code, message: this.message, kind: this.kind};}
}

const schema = JSON.parse(readFileSync(new URL('./schemas/contract.json', import.meta.url), 'utf8'));
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
  if (value && typeof value === 'object' && 'protocol_version' in value && value.protocol_version !== PROTOCOL_VERSION)
    throw new ProtocolError('UNSUPPORTED_PROTOCOL', 'Unsupported protocol version');
  if (!Object.hasOwn(schema.$defs, kind)) throw new Error(`Unknown contract type: ${kind}`);
  let check = validators.get(kind);
  if (!check) {check = ajv.getSchema(`${schema.$id}#/$defs/${kind}`)!; validators.set(kind, check);}
  if (!check(value)) throw new ProtocolError('INVALID_MESSAGE', `Invalid ${kind} shape`);
  const record = value as JsonObject;
  if (kind === 'environment_plan') validatePlan(record as EnvironmentPlan);
  if (kind === 'evaluation_result' || kind === 'aggregate_result') {
    const metrics = (record.metrics as JsonObject[]).map(item => String(item.name));
    if (new Set(metrics).size !== metrics.length)
      throw new ProtocolError('DUPLICATE_METRIC', 'Metric identities must be unique');
    if (record.primary_metric != null && !metrics.includes(String(record.primary_metric)))
      throw new ProtocolError('INVALID_METRIC', 'Primary metric is not reported');
  }
  return value as T;
}

function validatePlan(plan: EnvironmentPlan): void {
  const resources = new Map(plan.resources.map(item => [String(item.id), item]));
  if (resources.size !== plan.resources.length)
    throw new ProtocolError('DUPLICATE_RESOURCE', 'Resource identities must be unique');
  const visiting = new Set<string>(), visited = new Set<string>();
  const walk = (id: string) => {
    if (visiting.has(id)) throw new ProtocolError('RESOURCE_CYCLE', 'Resource dependencies contain a cycle');
    if (visited.has(id)) return;
    const item = resources.get(id);
    if (!item) throw new ProtocolError('MISSING_RESOURCE', 'Resource dependency is not declared');
    visiting.add(id);
    for (const dependency of (item.depends_on ?? []) as string[]) walk(dependency);
    visiting.delete(id); visited.add(id);
  };
  for (const id of resources.keys()) walk(id);
  const outputs = plan.outputs.map(item => String(item.id));
  if (new Set(outputs).size !== outputs.length)
    throw new ProtocolError('DUPLICATE_RESOURCE', 'Output identities must be unique');
  if (plan.outputs.some(item => !resources.has(String(item.resource_id))))
    throw new ProtocolError('MISSING_RESOURCE', 'Output resource is not declared');
}

export function validateConfig(schema: JsonObject, config: JsonObject): JsonObject {
  jsonValue(schema); jsonValue(config);
  let valid: boolean;
  try {
    if (schema.$schema !== undefined && schema.$schema !== 'https://json-schema.org/draft/2020-12/schema')
      throw new Error('Unsupported schema dialect');
    const check = (value: JsonValue): void => {
      if (value && typeof value === 'object') {
        if (!Array.isArray(value)) for (const key of ['$ref', '$dynamicRef'])
          if (key in value && (typeof value[key] !== 'string' || !value[key].startsWith('#')))
            throw new Error('Only local schema references are allowed');
        for (const child of Object.values(value)) check(child);
      }
    };
    check(schema);
    valid = Boolean(new Ajv2020({strict: false, validateFormats: false}).compile(schema)(config));
  } catch {throw new ProtocolError('INVALID_CONFIG', 'Invalid connector configuration schema');}
  if (!valid) throw new ProtocolError('INVALID_CONFIG', 'Configuration does not match connector schema');
  return config;
}

export function validateSpec(manifest: Manifest, spec: TrialSpec): TrialSpec {
  validate('manifest', manifest); validate('trial_spec', spec);
  validateConfig(manifest.config_schema, spec.config);
  for (const model of Object.values(spec.models) as JsonObject[]) {
    let valid = false;
    try {
      const endpoint = new URL(String(model.endpoint));
      valid = ['http:', 'https:'].includes(endpoint.protocol) && Boolean(endpoint.host) &&
        !endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash;
    } catch {}
    if (!valid) throw new ProtocolError('INVALID_CONFIG', 'Model endpoint contains invalid or private URL components');
    if (!Object.hasOwn(spec.credential_refs, String(model.credential_ref)))
      throw new ProtocolError('CREDENTIAL_REQUIRED', 'Model credential reference is unavailable');
  }
  return spec;
}

export function command(id: string, method: string, params: JsonObject = {}): WireMessage {
  return validate('command', {protocol_version: PROTOCOL_VERSION, type: 'command', id, method, params});
}
export function encodeLine(value: WireMessage): Buffer {
  validate('wire_message', value);
  const data = Buffer.from(JSON.stringify(value), 'utf8');
  if (data.length > MAX_LINE_BYTES) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
  return Buffer.concat([data, Buffer.from('\n')]);
}
export function decodeLine(line: Uint8Array): WireMessage {
  if (line.length > MAX_LINE_BYTES + 2) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
  if (line.at(-1) !== 10) throw new ProtocolError('INVALID_FRAME', 'Protocol record must end with LF');
  const payload = line.slice(0, line.at(-2) === 13 ? -2 : -1);
  if (payload.length > MAX_LINE_BYTES) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
  let value: unknown;
  try {value = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(payload));}
  catch {throw new ProtocolError('INVALID_FRAME', 'Invalid JSON protocol record');}
  return validate('wire_message', value);
}

export async function* lines(stream: AsyncIterable<Uint8Array>): AsyncGenerator<Buffer> {
  let buffer = Buffer.alloc(0);
  for await (const chunk of stream) {
    buffer = Buffer.concat([buffer, chunk]);
    let pos: number;
    while ((pos = buffer.indexOf(10)) !== -1) {
      if (pos > MAX_LINE_BYTES + 1) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
      yield buffer.subarray(0, pos + 1); buffer = buffer.subarray(pos + 1);
    }
    if (buffer.length > MAX_LINE_BYTES + 1) throw new ProtocolError('FRAME_TOO_LARGE', 'Protocol record exceeds size limit');
  }
  if (buffer.length) throw new ProtocolError('INVALID_FRAME', 'Protocol stream ended mid-record');
}
