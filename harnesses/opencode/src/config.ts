import { readFileSync } from 'node:fs';
import {
  ProtocolError,
  validateRun,
  type JsonObject,
  type Manifest,
  type RunSpec,
} from '@evalhive/harness-sdk';

export const manifest = JSON.parse(
  readFileSync(new URL('../manifest.json', import.meta.url), 'utf8'),
) as Manifest;
export const ADAPTER_VERSION = '0.1.0';
export function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function effectiveConfig(spec: RunSpec): JsonObject {
  validateRun(manifest, spec);
  return {
    contextWindow: 128000,
    maxOutputTokens: 8192,
    autoCompaction: true,
    reasoning: false,
    permissionMode: 'allow',
    ...spec.config,
  };
}
export function connection(spec: RunSpec, credentials: Record<string, string>) {
  const model = object(spec.model);
  if (typeof model.name !== 'string' || !model.name || typeof model.endpoint !== 'string')
    throw new ProtocolError('MODEL_REQUIRED', 'OpenCode requires an explicit model connection');
  if (model.api !== undefined && model.api !== 'openai-compatible')
    throw new ProtocolError(
      'UNSUPPORTED_MODEL_API',
      'This connector requires an OpenAI compatible connection',
    );
  let endpoint: URL;
  try {
    endpoint = new URL(model.endpoint);
  } catch {
    throw new ProtocolError('INVALID_MODEL_ENDPOINT', 'Model endpoint is invalid');
  }
  if (
    !['http:', 'https:'].includes(endpoint.protocol) ||
    endpoint.username ||
    endpoint.password ||
    endpoint.search ||
    endpoint.hash
  )
    throw new ProtocolError(
      'INVALID_MODEL_ENDPOINT',
      'Model endpoint must be an HTTP URL without inline credentials',
    );
  if (typeof model.credential_ref !== 'string' || !credentials[model.credential_ref])
    throw new ProtocolError('CREDENTIAL_REQUIRED', 'Model credential is unavailable');
  return {
    name: model.name,
    endpoint: model.endpoint.replace(/\/$/, ''),
    key: credentials[model.credential_ref],
  };
}
