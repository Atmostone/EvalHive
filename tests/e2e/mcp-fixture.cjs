// Self-contained stdio MCP fixture, injected into the isolated task environment.
const readline = require('node:readline');
const input = readline.createInterface({input: process.stdin});
input.on('line', line => {
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  let result;
  if (request.method === 'initialize') result = {
    protocolVersion: '2024-11-05', capabilities: {tools: {}}, serverInfo: {name: 'fixture', version: '1'},
  };
  else if (request.method === 'tools/list') result = {tools: [{name: 'echo', description: 'Echo fixture evidence',
    inputSchema: {type: 'object', properties: {text: {type: 'string'}}, required: ['text']}}]};
  else if (request.method === 'tools/call') result = {content: [{type: 'text', text: request.params.arguments.text}]};
  else result = {};
  process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: request.id, result}) + '\n');
});
