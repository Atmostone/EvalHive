import {createServer, type Server as HTTPServer} from 'node:http';
import {randomUUID} from 'node:crypto';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import {Server} from '@modelcontextprotocol/sdk/server/index.js';
import {StreamableHTTPServerTransport} from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import {CallToolRequestSchema, ListToolsRequestSchema, type Tool} from '@modelcontextprotocol/sdk/types.js';
import {ProtocolError, contentHash, type ExternalToolGate, type JsonObject} from '@evalhive/harness-sdk';
import type {Config} from '@opencode-ai/sdk/v2';

export const ROUTING_KEY = '__evalhive_native_call_id';

/** Discovery uses MCP; all effects are returned exclusively by the trial driver. */
export async function externalResources(mcp: NonNullable<Config['mcp']>, gate: ExternalToolGate, signal: AbortSignal) {
  const clients: Client[] = [], servers: HTTPServer[] = [], peers = new Set<Server>();
  const mapping: JsonObject[] = [], configured: NonNullable<Config['mcp']> = {};
  const close = async () => {
    await Promise.allSettled([...peers].map(peer => peer.close()));
    for (const server of servers) {server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve()));}
    await Promise.allSettled(clients.map(client => client.close()));
  };
  try {
    for (const [identity, config] of Object.entries(mcp)) {
      if (!('type' in config) || config.enabled === false) {configured[identity] = config; continue;}
      const client = new Client({name: 'evalhive-external-tools', version: '0.1.0'}); clients.push(client);
      const transport = config.type === 'local' ? new StdioClientTransport({command: config.command[0],
        args: config.command.slice(1), cwd: config.cwd, env: config.environment, stderr: 'ignore'}) :
        new StreamableHTTPClientTransport(new URL(config.url), {requestInit: {headers: config.headers}});
      await client.connect(transport, {signal, timeout: 30000});
      const tools: Tool[] = [];
      const exposed = (name: string) => `${identity}_${name.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
      let cursor: string | undefined;
      const cursors = new Set<string>();
      do {
        const page = await client.listTools(cursor ? {cursor} : {}, {signal, timeout: 30000});
        tools.push(...page.tools); cursor = page.nextCursor;
        if (tools.length > 256) throw new ProtocolError('RESOURCE_LIMIT', 'MCP server exposes too many tools');
        if (cursor) {
          if (cursors.has(cursor) || cursors.size >= 256) throw new ProtocolError('RESOURCE_LIMIT', 'MCP tool pagination did not finish');
          cursors.add(cursor);
        }
      } while (cursor);
      for (const tool of tools) {
        if (exposed(tool.name).length > 64) throw new ProtocolError('UNSUPPORTED_TOOL', 'External tool name exceeds the native provider limit');
        if (mapping.some(item => item.exposed_name === exposed(tool.name)))
          throw new ProtocolError('INVALID_RESOURCE', 'MCP exposed tool names collide');
        if (Object.hasOwn(tool.inputSchema.properties ?? {}, ROUTING_KEY))
          throw new ProtocolError('INVALID_RESOURCE', 'MCP tool schema uses a reserved routing key');
        mapping.push({native_name: tool.name, exposed_name: exposed(tool.name),
          canonical_name: `mcp__${identity}__${tool.name}`, schema_sha256: contentHash(tool.inputSchema as JsonObject),
          exposed_schema_sha256: contentHash({...tool.inputSchema, properties: tool.inputSchema.properties ?? {}, additionalProperties: false} as JsonObject)});
      }
      const route = '/' + randomUUID();
      const server = createServer(async (request, response) => {
        if (request.method !== 'POST' || request.url !== route) {response.writeHead(405); response.end(); return;}
        if (peers.size >= 128) {response.writeHead(429); response.end(); return;}
        const peer = new Server({name: 'evalhive-external-tools', version: '0.1.0'},
          {capabilities: {tools: {}}, instructions: client.getInstructions()});
        peers.add(peer);
        const native = new StreamableHTTPServerTransport({sessionIdGenerator: undefined, enableJsonResponse: true});
        peer.setRequestHandler(ListToolsRequestSchema, async () => ({tools}));
        peer.setRequestHandler(CallToolRequestSchema, async ({params}) => {
          const args = {...params.arguments} as JsonObject;
          const callId = args[ROUTING_KEY]; delete args[ROUTING_KEY];
          if (typeof callId !== 'string') throw new ProtocolError('TOOL_ID_MISSING', 'Native tool routing identity is missing');
          const result = await gate.execute(callId, exposed(params.name), args);
          return {content: [{type: 'text', text: typeof result.content === 'string' ? result.content : JSON.stringify(result.content)}],
            isError: Boolean(result.is_error)};
        });
        response.once('close', () => {peers.delete(peer); void peer.close();});
        try {await peer.connect(native); await native.handleRequest(request, response);}
        catch {if (!response.headersSent) response.writeHead(500); response.end();}
      });
      servers.push(server);
      await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
      const address = server.address();
      if (!address || typeof address === 'string') throw new ProtocolError('MCP_UNAVAILABLE', 'External tool gateway did not start');
      configured[identity] = {type: 'remote', url: `http://127.0.0.1:${address.port}${route}`, oauth: false,
        enabled: true, timeout: Math.max(config.timeout ?? 30000, 300000)};
    }
    return {mcp: configured, mapping, close};
  } catch (error) {await close(); throw error;}
}
