// Local protocol fixture for pinned native harnesses. It never calls a model.
import { createServer } from 'node:http';

export function stream(res, index, {content = '', tool, tools = tool ? [tool] : undefined, usage = true} = {}) {
  res.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'});
  const base = {id: `fake-${index}`, object: 'chat.completion.chunk', created: 1, model: 'fake-model'};
  const send = (value) => res.write(`data: ${JSON.stringify({...base, ...value})}\n\n`);
  send({choices: [{index: 0, delta: {role: 'assistant', ...(tools ? {tool_calls: tools.map((call, index) => ({index, ...call}))} : {content})}, finish_reason: null}]});
  send({choices: [{index: 0, delta: {}, finish_reason: tools ? 'tool_calls' : 'stop'}]});
  if (usage) send({choices: [], usage: typeof usage === 'object' ? usage : {prompt_tokens: index * 10, completion_tokens: index * 2,
    total_tokens: index * 12, prompt_tokens_details: {cached_tokens: 2}, completion_tokens_details: {reasoning_tokens: 1}}});
  res.end('data: [DONE]\n\n');
}

export async function fakeOpenAI(handler) {
  const requests = [];
  const waiters = [];
  const server = createServer(async (req, res) => {
    try {
      let raw = '';
      for await (const chunk of req) raw += chunk;
      const body = JSON.parse(raw);
      requests.push(body);
      for (const waiter of waiters.splice(0)) waiter();
      await handler(body, requests.length, res, req);
    } catch {
      if (!res.headersSent) res.writeHead(500, {'Content-Type': 'application/json'});
      res.end(JSON.stringify({error: {message: 'Fake endpoint fixture failed'}}));
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    url: `http://127.0.0.1:${server.address().port}/v1`, requests,
    async waitForRequest() {
      if (requests.length) return;
      await new Promise((resolve) => waiters.push(resolve));
    },
    async close() {
      server.closeAllConnections();
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    },
  };
}
