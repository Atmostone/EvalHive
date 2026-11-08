// Local-only model fixture for the release smoke; it accepts engineering prompts.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {stream} from './fake-openai.mjs';

const calls = new Map();
createServer(async (request, response) => {
  try {
    assert.equal(request.url, '/v1/chat/completions');
    assert.equal(request.headers.authorization, 'Bearer local-engineering-key');
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    const native = /^release-native-[a-f0-9]{32}-(pi|opencode)$/.test(body.model ?? '');
    const key = native ? body.model : JSON.stringify(body.messages).match(/EVALHIVE_RELEASE_SMOKE:([a-z0-9-]+)/)?.[1];
    assert(key, 'Only release engineering fixtures are accepted');
    const index = (calls.get(key) ?? 0) + 1;
    calls.set(key, index);
    if (native) {
      if (index === 1) {
        const tool = (body.tools ?? []).map(row => row.function).find(row => row.name === 'bash');
        assert(tool, 'The native shell tool is required');
        stream(response, index, {tool: {id: 'release-native-write', type: 'function', function: {
          name: tool.name, arguments: JSON.stringify({command: "printf 'Benchmark release artifact\\n' > /workspace/native-result.txt", description: 'Write the native release fixture artifact'}),
        }}});
      } else stream(response, index, {content: 'Completed',
        usage: {prompt_tokens: 20, completion_tokens: 3, prompt_tokens_details: {cached_tokens: 0}}});
      console.log(`Native engineering request ${key} ${index}`);
      return;
    }
    if (index === 1) {
      const tool = (body.tools ?? []).map(row => row.function).find(row => ['write', 'file_write'].includes(row.name));
      assert(tool, 'The portable file tool is required');
      const pathKey = Object.hasOwn(tool.parameters.properties, 'filePath') ? 'filePath' : 'path';
      stream(response, index, {tool: {id: 'release-write', type: 'function', function: {
        name: tool.name, arguments: JSON.stringify({[pathKey]: '/workspace/result.txt', content: 'Release engineering artifact\n'}),
      }}});
    } else stream(response, index, {content: 'Release engineering smoke completed',
      usage: {prompt_tokens: 20, completion_tokens: 3, prompt_tokens_details: {cached_tokens: 0}}});
    console.log(`Engineering request ${key} ${index}`);
  } catch {
    if (!response.headersSent) response.writeHead(400, {'Content-Type': 'application/json'});
    response.end(JSON.stringify({error: {message: 'Release fixture request rejected'}}));
  }
}).listen(4100, '0.0.0.0');
