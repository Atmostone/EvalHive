// Stateless Streamable HTTP MCP fixture, compatible with the backend endpoint.
import { createServer } from 'node:http';

export async function fakeMcpHTTP(authorize = () => {}, tools = [{name: 'search_knowledge_base',
  description: 'Search fixture data', inputSchema: {type: 'object', properties: {query: {type: 'string'}}, required: ['query']}}]) {
  const requests = [];
  const server = createServer(async (req, res) => {
    if (req.method !== 'POST') { res.writeHead(405); res.end(); return; }
    try {
      authorize(req);
      let input = '';
      for await (const chunk of req) input += chunk;
      const body = JSON.parse(input);
      requests.push(body);
      if (body.id === undefined) { res.writeHead(202); res.end(); return; }
      let result;
      if (body.method === 'initialize') result = {protocolVersion: body.params.protocolVersion,
        capabilities: {tools: {}}, serverInfo: {name: 'evalhive-knowledge-fixture', version: '1'}};
      else if (body.method === 'tools/list') result = {tools};
      else if (body.method === 'tools/call') result = {content: [{type: 'text', text: 'Knowledge fixture result'}]};
      else throw new Error('Unsupported fixture method');
      res.writeHead(200, {'Content-Type': 'application/json'});
      res.end(JSON.stringify({jsonrpc: '2.0', id: body.id, result}));
    } catch {
      res.writeHead(500); res.end();
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {url: `http://127.0.0.1:${server.address().port}/mcp`, requests,
    async close() {
      server.closeAllConnections();
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }};
}
