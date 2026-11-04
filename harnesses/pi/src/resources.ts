import { accessSync, constants } from 'node:fs';
import { createHash } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { McpClient, StdioTransport, StreamableHttpTransport, toLlmContent } from '@earendil-works/pi-mcp';
import { loadSkills, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { ProtocolError, resourceConfig, resources, type JsonObject, type RunSpec } from '@evalhive/harness-sdk';
import { object } from './config.js';

const builtinAliases: Record<string, string> = {file_read: 'read', file_write: 'write'};
const builtinNames = ['read', 'bash', 'edit', 'write', 'grep', 'find', 'ls'];

export function modelToolName(canonical: string): string {
  if (/^[A-Za-z0-9_-]{1,64}$/.test(canonical)) return canonical;
  return canonical.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 47) + '_' +
    createHash('sha256').update(canonical).digest('hex').slice(0, 16);
}

export function resourcePlan(spec: RunSpec, credentials: Record<string, string>) {
  const values = resources(spec);
  const toolset = values.find((value) => value.kind === 'tools');
  const tools = toolset ? (toolset.config.names as string[]).map((name) => builtinAliases[name] ?? name) : undefined;
  const skills = values.filter((value) => value.kind === 'skill').map((value) => String(value.config.path));
  for (const path of skills) {
    if (!isAbsolute(path)) throw new ProtocolError('INVALID_RESOURCE', 'Skill paths must be absolute');
    try { accessSync(path, constants.R_OK); }
    catch { throw new ProtocolError('RESOURCE_UNAVAILABLE', 'A configured skill is unavailable'); }
  }
  const instructions = values.filter((value) => value.kind === 'instructions' ||
    (value.kind === 'memory' && value.config.mode !== 'off'))
    .map((value) => String(value.config.content ?? ''));
  const mcp = values.filter((value) => value.kind === 'mcp').map((value) => {
    if (!/^[A-Za-z0-9_-]+$/.test(value.id))
      throw new ProtocolError('INVALID_RESOURCE', 'MCP identities must be valid tool namespaces');
    const config = resourceConfig(value, credentials);
    if (config.transport === 'http') {
      let url: URL;
      try { url = new URL(String(config.url)); }
      catch { throw new ProtocolError('INVALID_RESOURCE', 'MCP endpoint is invalid'); }
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash)
        throw new ProtocolError('INVALID_RESOURCE', 'MCP endpoint must not contain inline credentials');
    }
    return {id: value.id, config};
  });
  for (const name of tools ?? []) {
    const available = builtinNames.includes(name) || mcp.some((server) =>
      name.startsWith(`mcp__${server.id}__`) || Object.values(object(server.config.aliases)).includes(name));
    if (!available) throw new ProtocolError('UNSUPPORTED_TOOL', 'A requested tool is unavailable in Pi');
  }
  return {tools, skills, instructions, mcp};
}

export async function connectResources(spec: RunSpec, credentials: Record<string, string>, signal: AbortSignal) {
  const plan = resourcePlan(spec, credentials);
  const clients: McpClient[] = [];
  const tools: ToolDefinition[] = [];
  const aliases: Record<string, string[]> = {read: ['file_read'], write: ['file_write']};
  const close = async () => { await Promise.allSettled(clients.map((client) => client.close())); };
  try {
    if (plan.tools?.length !== 0) for (const server of plan.mcp) {
      const config = server.config;
      const client = new McpClient({name: 'evalhive-pi', version: '0.1.0',
        requestTimeoutMs: Number(config.timeout_seconds ?? 30) * 1000});
      clients.push(client);
      const transport = config.transport === 'stdio' ? new StdioTransport({command: String(config.command),
        args: config.args as string[] | undefined, cwd: String(config.cwd ?? spec.cwd),
        env: config.env as Record<string, string> | undefined, inheritEnv: true, stderr: 'pipe',
        maxMessageBytes: 1024 * 1024, maxStderrBytes: 65536}) :
        new StreamableHttpTransport({url: String(config.url), headers: config.headers as Record<string, string> | undefined,
          maxMessageBytes: 1024 * 1024});
      await client.connect(transport);
      if (client.instructions) plan.instructions.push(client.instructions);
      const discovered = await client.listTools({signal});
      if (discovered.length > 256) throw new ProtocolError('RESOURCE_LIMIT', 'MCP server exposes too many tools');
      for (const tool of discovered) {
        const canonical = `mcp__${server.id}__${tool.name}`;
        const name = modelToolName(canonical);
        const alias = object(config.aliases)[tool.name];
        aliases[name] = [...new Set([...(typeof alias === 'string' ? [alias] : []),
          ...(typeof config.alias_prefix === 'string' ? [config.alias_prefix + tool.name] : []), canonical])];
        tools.push({name, label: name, description: tool.description ?? name,
          parameters: tool.inputSchema as ToolDefinition['parameters'],
          async execute(_callId, args, toolSignal) {
            const result = await client.callTool(tool.name, args as Record<string, unknown>, {signal: toolSignal ?? signal});
            return {content: toLlmContent(result), details: result, isError: result.isError};
          }});
      }
    }
    const selected = plan.tools?.flatMap((name) => {
      if (name.endsWith('*')) {
        const matches = tools.filter((tool) => aliases[tool.name]?.some(alias => alias.startsWith(name.slice(0, -1)))).map((tool) => tool.name);
        if (!matches.length) throw new ProtocolError('UNSUPPORTED_TOOL', 'A requested MCP namespace has no tools');
        return matches;
      }
      return tools.find((tool) => aliases[tool.name]?.includes(name))?.name ?? name;
    });
    for (const name of selected ?? []) if (![...builtinNames, ...tools.map((tool) => tool.name)].includes(name))
      throw new ProtocolError('UNSUPPORTED_TOOL', 'A requested tool is unavailable in Pi');
    return {...plan, tools: selected, customTools: tools, aliases, close,
      loadSkills(agentDir: string) {
        const result = loadSkills({cwd: spec.cwd, agentDir, skillPaths: plan.skills, includeDefaults: false});
        if (result.diagnostics.length) throw new ProtocolError('INVALID_SKILL', 'Configured skills have validation errors');
        return result;
      }};
  } catch (error) {
    await close();
    if (error instanceof ProtocolError) throw error;
    throw new ProtocolError('MCP_UNAVAILABLE', 'A configured MCP server could not be initialized');
  }
}
