// Local protocol fixture. It never contacts an external service.
import { createInterface } from 'node:readline';
for await (const line of createInterface({input: process.stdin})) {
  const request = JSON.parse(line);
  if (request.id === undefined) continue;
  let result;
  if (request.method === 'initialize') result = {protocolVersion: request.params.protocolVersion,
    capabilities: {tools: {}}, serverInfo: {name: 'evalhive-fixture', version: '1'}};
  else if (request.method === 'tools/list') result = {tools: [{name: 'echo', description: 'Echo fixture input',
    inputSchema: {type: 'object', properties: {text: {type: 'string'}}, required: ['text']}}]};
  else if (request.method === 'tools/call') result = {content: [{type: 'text', text: request.params.arguments.text}],
    isError: request.params.arguments.text === 'fixture-error'};
  else if (request.method === 'ping') result = {};
  else {
    process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: request.id, error: {code: -32601, message: 'Unknown method'}}) + '\n');
    continue;
  }
  process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: request.id, result}) + '\n');
}
