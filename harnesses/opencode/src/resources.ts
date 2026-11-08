import { accessSync, constants, statSync } from 'node:fs';
import { basename, dirname, isAbsolute } from 'node:path';
import {
  resourceConfig,
  resources,
  ProtocolError,
  type JsonObject,
  type RunSpec,
} from '@evalhive/harness-sdk';
import type { Config } from '@opencode-ai/sdk/v2';
import { object } from './config.js';

const builtinAliases: Record<string, string> = { file_read: 'read', file_write: 'write' };
const builtin = [
  'bash',
  'read',
  'write',
  'edit',
  'glob',
  'grep',
  'list',
  'webfetch',
  'websearch',
  'task',
  'todowrite',
  'skill',
  'question',
  'apply_patch',
];

export function resourcePlan(spec: RunSpec, credentials: Record<string, string>) {
  const values = resources(spec),
    mcp: NonNullable<Config['mcp']> = {},
    aliases: Record<string, string[]> = { read: ['file_read'], write: ['file_write'] };
  const selected = values.find((item) => item.kind === 'tools');
  const aliasPrefixes: Record<string, string> = {};
  const names = selected
    ? (selected.config.names as string[]).map((name) => builtinAliases[name] ?? name)
    : undefined;
  const instructions = values
    .filter((item) => item.kind === 'instructions' || (item.kind === 'memory' && item.config.mode !== 'off'))
    .map((item) => String(item.config.content ?? ''));
  const skills = values
    .filter((item) => item.kind === 'skill')
    .map((item) => {
      const path = String(item.config.path);
      if (!isAbsolute(path)) throw new ProtocolError('INVALID_RESOURCE', 'Skill paths must be absolute');
      try {
        accessSync(path, constants.R_OK);
      } catch {
        throw new ProtocolError('RESOURCE_UNAVAILABLE', 'A configured skill is unavailable');
      }
      if (statSync(path).isDirectory()) return path;
      if (statSync(path).isFile() && basename(path) === 'SKILL.md') return dirname(path);
      throw new ProtocolError('INVALID_SKILL', 'OpenCode requires a skill directory or SKILL.md');
    });
  for (const resource of values.filter((item) => item.kind === 'mcp')) {
    if (!/^[A-Za-z0-9_-]+$/.test(resource.id))
      throw new ProtocolError('INVALID_RESOURCE', 'MCP identity is invalid');
    const config = resourceConfig(resource, credentials);
    if (typeof config.alias_prefix === 'string') aliasPrefixes[resource.id] = config.alias_prefix;
    for (const [tool, alias] of Object.entries(object(config.aliases)))
      aliases[`${resource.id}_${tool}`] = [String(alias)];
    const common = { enabled: names?.length !== 0, timeout: Number(config.timeout_seconds ?? 30) * 1000 };
    if (config.transport === 'stdio')
      mcp[resource.id] = {
        type: 'local',
        command: [String(config.command), ...((config.args as string[]) ?? [])],
        cwd: String(config.cwd ?? spec.cwd),
        environment: config.env as Record<string, string> | undefined,
        ...common,
      };
    else {
      let url: URL;
      try {
        url = new URL(String(config.url));
      } catch {
        throw new ProtocolError('INVALID_RESOURCE', 'MCP endpoint is invalid');
      }
      if (
        !['http:', 'https:'].includes(url.protocol) ||
        url.username ||
        url.password ||
        url.search ||
        url.hash
      )
        throw new ProtocolError('INVALID_RESOURCE', 'MCP endpoint must not contain inline credentials');
      mcp[resource.id] = {
        type: 'remote',
        url: url.href,
        headers: config.headers as Record<string, string> | undefined,
        oauth: false,
        ...common,
      };
    }
  }
  const selection: Record<string, boolean> | undefined = names ? { '*': false } : undefined;
  for (const name of names ?? []) {
    if (object(spec.interaction).mode === 'external_steps' && builtin.includes(name))
      throw new ProtocolError('UNSUPPORTED_TOOL', 'External steps permit only driver-owned MCP tools');
    const server = Object.keys(mcp).find((id) => name.startsWith(`mcp__${id}__`));
    const native = server
      ? name.replace(`mcp__${server}__`, `${server}_`)
      : (Object.keys(aliases).find((tool) => aliases[tool].includes(name)) ?? name);
    if (!builtin.includes(native) && !server && !(native in aliases))
      throw new ProtocolError('UNSUPPORTED_TOOL', 'A requested tool is unavailable in OpenCode');
    selection![native] = true;
  }
  if (object(spec.interaction).mode === 'external_steps' && !selection) {
    return {mcp, aliases, aliasPrefixes, instructions, skills,
      selection: {'*': false, ...Object.fromEntries(Object.keys(mcp).map(id => [`${id}_*`, true]))}};
  }
  return { mcp, aliases, aliasPrefixes, selection, instructions, skills };
}
