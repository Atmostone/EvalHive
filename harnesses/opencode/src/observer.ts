import { createServer, type Server } from 'node:http';
import { once } from 'node:events';
import type { JsonObject } from '@evalhive/harness-sdk';
import { object } from './config.js';
import type { EventWriter } from './events.js';

const count = (value: unknown) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;

/** A byte-preserving local transport observer; OpenCode owns every model call. */
export class ProviderObserver {
  private server: Server | undefined;
  private requests = 0;
  private active = new Set<AbortController>();
  private handlers = new Set<Promise<void>>();
  url = '';
  lastStatus: number | undefined;
  constructor(
    private endpoint: string,
    private output: EventWriter,
    private interaction?: {
      beforeRequest: () => Promise<void>;
      tools: (messageId: string, calls: JsonObject[], content: string) => void;
    },
  ) {}

  async start(): Promise<void> {
    this.server = createServer(async (request, response) => {
      if (request.method !== 'POST' || request.url !== '/v1/chat/completions') {
        response.writeHead(404);
        response.end();
        return;
      }
      const controller = new AbortController();
      let done!: () => void;
      const completed = new Promise<void>((resolve) => {
        done = resolve;
      });
      this.handlers.add(completed);
      this.active.add(controller);
      response.once('close', () => controller.abort());
      let session: string | undefined;
      let state: JsonObject | undefined;
      let receivedResponse = false;
      try {
        const chunks: Buffer[] = [];
        let bytes = 0;
        for await (const chunk of request) {
          bytes += chunk.length;
          if (bytes > 16 * 1024 * 1024) throw new Error('Request exceeds the observer limit');
          chunks.push(chunk);
        }
        const body = Buffer.concat(chunks);
        const input = object(JSON.parse(body.toString('utf8')));
        session =
          String(request.headers['x-evalhive-session'] ?? request.headers['x-opencode-session-id'] ?? '') ||
          undefined;
        const nativePurpose = String(request.headers['x-evalhive-purpose'] ?? 'unknown');
        const purpose = ['build', 'evalhive', 'general', 'explore'].includes(nativePurpose)
          ? 'agent'
          : nativePurpose;
        if (this.interaction && purpose === 'agent') await this.interaction.beforeRequest();
        state = {
          usage_id: `request-${++this.requests}`,
          mode: 'cumulative',
          model: typeof input.model === 'string' ? input.model : null,
          purpose,
          input_tokens: null,
          output_tokens: null,
          cache_read_tokens: null,
          cache_write_tokens: null,
          reasoning_tokens: null,
          cost: null,
        };
        this.output.emit('usage', { ...state }, session);
        this.output.native(
          'provider.request',
          {
            request_id: state.usage_id,
            requested_model: input.model,
            purpose: nativePurpose,
            stream: input.stream,
          },
          session,
        );
        const headers = new Headers();
        headers.set('accept-encoding', 'identity');
        for (const [name, value] of Object.entries(request.headers)) {
          if (
            value === undefined ||
            ['host', 'connection', 'transfer-encoding', 'content-length', 'accept-encoding'].includes(name) ||
            name.startsWith('x-evalhive-')
          )
            continue;
          headers.set(name, Array.isArray(value) ? value.join(', ') : value);
        }
        const upstream = await fetch(this.endpoint + '/chat/completions', {
          method: 'POST',
          headers,
          body,
          signal: controller.signal,
          redirect: 'error',
        });
        receivedResponse = true;
        this.lastStatus = upstream.status;
        if (upstream.status >= 400) this.output.emit('failure.observed', {
          request_id: state.usage_id, purpose,
          facts: {kind: 'model_error', status_code: upstream.status, exception: null},
        }, session);
        response.writeHead(upstream.status, {
          'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
        });
        this.output.native(
          'provider.response',
          { request_id: state.usage_id, status: upstream.status },
          session,
        );
        const decoder = new TextDecoder();
        let buffer = '';
        const toolCalls = new Map<number, {id: string; name: string; arguments: string}>();
        let toolBytes = 0, content = '', announced = false;
        const observe = (packet: unknown) => {
          const value = object(packet);
          if (this.interaction && purpose === 'agent') {
            const choice = object(Array.isArray(value.choices) ? value.choices[0] : undefined);
            const delta = object(choice.delta ?? choice.message);
            if (typeof delta.content === 'string') content += delta.content;
            for (const raw of Array.isArray(delta.tool_calls) ? delta.tool_calls : []) {
              const call = object(raw), fn = object(call.function), index = Number(call.index ?? toolCalls.size);
              const saved = toolCalls.get(index) ?? {id: '', name: '', arguments: ''};
              if (typeof call.id === 'string') saved.id += call.id;
              if (typeof fn.name === 'string') saved.name += fn.name;
              if (typeof fn.arguments === 'string') {saved.arguments += fn.arguments; toolBytes += fn.arguments.length;}
              toolCalls.set(index, saved);
            }
            if (toolCalls.size > 128 || toolBytes + content.length > 1024 * 1024) throw new Error('Native tool batch exceeds the limit');
            if (choice.finish_reason === 'tool_calls' && !announced) {
              const identity = request.headers['x-evalhive-message'];
              if (typeof identity !== 'string' || !identity) throw new Error('Native model message identity is missing');
              const calls = [...toolCalls.entries()].sort(([a], [b]) => a - b).map(([,call]) => ({call_id: call.id,
                name: call.name, arguments: JSON.parse(call.arguments) as JsonObject}));
              if (calls.length) this.interaction.tools(identity, calls, content);
              announced = true;
            }
          }
          if (typeof value.model === 'string') state!.metadata = {model_source: 'request', reported_model: value.model};
          const usage = object(value.usage);
          if (!Object.keys(usage).length) return;
          for (const [wire, name] of [
            ['prompt_tokens', 'input_tokens'],
            ['completion_tokens', 'output_tokens'],
          ] as const)
            if (Object.hasOwn(usage, wire)) state![name] = count(usage[wire]);
          const prompt = object(usage.prompt_tokens_details),
            completion = object(usage.completion_tokens_details);
          if (Object.hasOwn(prompt, 'cached_tokens')) state!.cache_read_tokens = count(prompt.cached_tokens);
          if (Object.hasOwn(completion, 'reasoning_tokens'))
            state!.reasoning_tokens = count(completion.reasoning_tokens);
          this.output.native(
            'provider.usage',
            { request_id: state!.usage_id, model: state!.model, usage: usage as JsonObject },
            session,
          );
          this.output.emit('usage', { ...state! }, session);
        };
        if (upstream.body)
          for await (const chunk of upstream.body) {
            if (!response.write(chunk))
              await Promise.race([once(response, 'drain'), once(response, 'close')]);
            if (response.destroyed) break;
            buffer += decoder.decode(chunk, { stream: true });
            if (buffer.length > 1024 * 1024) throw new Error('Response frame exceeds the observer limit');
            if (upstream.headers.get('content-type')?.includes('text/event-stream')) {
              buffer = buffer.replace(/\r\n/g, '\n');
              let end: number;
              while ((end = buffer.indexOf('\n\n')) !== -1) {
                const frame = buffer.slice(0, end);
                buffer = buffer.slice(end + 2);
                const payload = frame
                  .split('\n')
                  .filter((line) => line.startsWith('data:'))
                  .map((line) => line.slice(5).trimStart())
                  .join('\n');
                if (payload && payload !== '[DONE]') observe(JSON.parse(payload));
              }
            }
          }
        if (upstream.ok && upstream.headers.get('content-type')?.includes('application/json') && buffer)
          observe(JSON.parse(buffer));
        if (state) this.output.emit('usage', { ...state }, session);
        response.end();
      } catch {
        if (!controller.signal.aborted) {
          if (state && !receivedResponse) {
            // Preserve native retry policy after an upstream connection failure.
            this.output.emit('failure.observed', {request_id: state.usage_id, purpose: state.purpose,
              facts: {kind: 'model_error', status_code: null, exception: 'APIConnectionError'}}, session);
          } else this.output.fail('PROVIDER_RECORDING_FAILED', 'Provider transport could not be observed completely');
          if (!response.headersSent) response.writeHead(502, { 'Content-Type': 'application/json' });
          response.end(JSON.stringify({ error: { message: 'EvalHive provider transport failed' } }));
        }
      } finally {
        controller.abort();
        this.active.delete(controller);
        this.handlers.delete(completed);
        done();
      }
    });
    await new Promise<void>((resolve) => this.server!.listen(0, '127.0.0.1', resolve));
    const address = this.server.address();
    if (!address || typeof address === 'string') throw new Error('Observer address is unavailable');
    this.url = `http://127.0.0.1:${address.port}/v1`;
  }
  async close(): Promise<void> {
    for (const controller of this.active) controller.abort();
    this.server?.closeAllConnections();
    await Promise.allSettled([...this.handlers]);
    if (this.server) await new Promise<void>((resolve) => this.server!.close(() => resolve()));
    this.server = undefined;
  }
}
