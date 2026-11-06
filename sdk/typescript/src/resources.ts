import { ProtocolError, validate, type JsonObject, type RunSpec } from './protocol.js';

export type Resource = JsonObject & {id: string; kind: string; config: JsonObject;
  credential_refs?: Record<string, string>};

/** Resource semantics are shared; native names and configuration stay in connectors. */
export function resources(spec: RunSpec): Resource[] {
  const environment = spec.environment as JsonObject | undefined;
  const values = (environment?.resources ?? []) as Resource[];
  const seen = new Set<string>();
  const singleton = new Set<string>();
  for (const resource of values) {
    validate('resource', resource);
    if (seen.has(resource.id) || (['tools', 'memory'].includes(resource.kind) && singleton.has(resource.kind)))
      throw new ProtocolError('DUPLICATE_RESOURCE', 'Resource identities and singleton kinds must be unique');
    seen.add(resource.id); singleton.add(resource.kind);
  }
  return values;
}

/** Credentials are resolved only inside the worker, never in the public RunSpec. */
export function resourceConfig(resource: Resource, credentials: Record<string, string>): JsonObject {
  validate('resource', resource);
  const config = structuredClone(resource.config);
  for (const [target, reference] of Object.entries(resource.credential_refs ?? {})) {
    if (typeof credentials[reference] !== 'string')
      throw new ProtocolError('CREDENTIAL_REQUIRED', 'Resource credential is unavailable');
    const [section, name] = target.split('.');
    if ((section === 'env') !== (config.transport === 'stdio'))
      throw new ProtocolError('INVALID_RESOURCE', 'Credential target does not match the MCP transport');
    config[section] = {...config[section] as JsonObject ?? {}, [name]: credentials[reference]};
  }
  return config;
}
