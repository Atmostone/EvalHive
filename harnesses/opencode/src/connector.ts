import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createOpencodeClient, type Config, type OpencodeClient } from '@opencode-ai/sdk/v2';
import {
  ProtocolError,
  ExternalToolGate,
  conversationHistory,
  openModelGateway,
  type Connector,
  type JsonObject,
  type JsonValue,
  type RunContext,
  type RunResult,
  type RunSpec,
} from '@evalhive/harness-sdk';
import { ADAPTER_VERSION, connection, effectiveConfig, manifest, object } from './config.js';
import { EventTranslator, EventWriter } from './events.js';
import { ProviderObserver } from './observer.js';
import { resourcePlan } from './resources.js';
import { executable, isolatedEnv, startServer, stopServer } from './server.js';
import {externalResources} from './external-tools.js';
import {nativeHistory} from './history.js';

const runFile = promisify(execFile);
const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class OpenCodeConnector implements Connector {
  readonly manifest = manifest;
  private client: OpencodeClient | undefined;
  private sessionId: string | undefined;
  private stopped = false;
  private pendingFeedback: string[] = [];
  private controls = 0;
  private nativeVersion: Promise<void> | undefined;
  private permissions = new Map<string, string>();
  private feedbackGeneration = 0;
  private started = false;
  private apiTimeoutMs = 30000;
  private runSignal: AbortSignal | undefined;
  private promptOptions: { model: { providerID: string; modelID: string }; agent: string } | undefined;

  constructor(
    private credentials: Record<string, string>,
    private transport: { fetch?: typeof fetch } = {},
  ) {}

  private apiSignal(): AbortSignal {
    const timeout = AbortSignal.timeout(this.apiTimeoutMs);
    return this.runSignal ? AbortSignal.any([this.runSignal, timeout]) : timeout;
  }

  async validate(spec: RunSpec): Promise<JsonValue> {
    const config = effectiveConfig(spec);
    connection(spec, this.credentials);
    resourcePlan(spec, this.credentials);
    conversationHistory(spec);
    if (Number(config.maxOutputTokens) > Number(config.contextWindow))
      throw new ProtocolError('INVALID_LIMITS', 'Output limit cannot exceed the context window');
    this.nativeVersion ??= this.checkVersion();
    await this.nativeVersion;
    return { accepted: true, effective_config: config };
  }

  private async checkVersion(): Promise<void> {
    const state = mkdtempSync(join(tmpdir(), 'evalhive-opencode-version-'));
    try {
      const { stdout } = await runFile(executable, ['--version'], {
        env: isolatedEnv(state),
        timeout: 10000,
        maxBuffer: 65536,
      });
      if (stdout.trim() !== '1.19.5')
        throw new ProtocolError(
          'NATIVE_VERSION_MISMATCH',
          'Installed OpenCode version does not match the connector',
        );
    } catch (error) {
      if (error instanceof ProtocolError) throw error;
      throw new ProtocolError('NATIVE_UNAVAILABLE', 'The pinned OpenCode executable is unavailable');
    } finally {
      rmSync(state, { recursive: true, force: true });
    }
  }

  private async interrupt(): Promise<void> {
    if (this.client && this.sessionId)
      await this.client.session
        .abort({ sessionID: this.sessionId }, { throwOnError: true, signal: AbortSignal.timeout(5000) })
        .catch(() => {});
  }

  private async sendPrompt(text: string): Promise<void> {
    const client = this.client!,
      sessionID = this.sessionId!;
    const before = await client.session.messages(
      { sessionID },
      { throwOnError: true, signal: this.apiSignal() },
    );
    const known = new Set(before.data!.map((entry) => entry.info.id));
    await client.session.promptAsync(
      { sessionID, ...this.promptOptions, parts: [{ type: 'text', text }] },
      { throwOnError: true, signal: AbortSignal.timeout(5000) },
    );
    // The native 202 response admits background work; acknowledge feedback only
    // after its user message is visible in the persisted native transcript.
    const deadline = Date.now() + this.apiTimeoutMs;
    while (Date.now() < deadline) {
      const response = await client.session.messages(
        { sessionID },
        { throwOnError: true, signal: this.apiSignal() },
      );
      if (
        response.data!.some(
          (entry) =>
            entry.info.role === 'user' &&
            !known.has(entry.info.id) &&
            entry.parts.some((part) => part.type === 'text' && part.text === text),
        )
      )
        return;
      await delay(50);
    }
    throw new ProtocolError('INPUT_ACK_UNKNOWN', 'OpenCode did not confirm its persisted input');
  }

  async run(context: RunContext): Promise<RunResult> {
    const config = effectiveConfig(context.spec),
      model = connection(context.spec, this.credentials);
    const plan = resourcePlan(context.spec, this.credentials);
    const state = mkdtempSync(join(tmpdir(), 'evalhive-opencode-'));
    const limitPath = join(state, 'limit.json');
    const streamAbort = new AbortController();
    const output = new EventWriter(context, () => {
      void this.interrupt();
    });
    const translator = new EventTranslator(output, plan.aliases, plan.aliasPrefixes);
    const gate = context.conversation?.mode === 'external_steps' ? new ExternalToolGate(context.conversation, context.signal) : undefined;
    let batch: Promise<unknown> = Promise.resolve();
    let externalFailure: unknown;
    const observer = new ProviderObserver(model.endpoint, output, gate ? {
      beforeRequest: async () => {await batch; if (externalFailure) throw externalFailure;},
      tools: (messageId, calls, content) => {
        if (calls.some(call => !resourceSet?.mapping.some(tool => tool.exposed_name === call.name)))
          throw new ProtocolError('UNSUPPORTED_TOOL_STEP', 'Native model requested an unavailable external tool');
        batch = gate.announce(messageId, messageId, calls, content).catch(error => {externalFailure = error; void this.interrupt();});
      },
    } : undefined);
    this.apiTimeoutMs = Number(config.apiTimeoutSeconds) * 1000;
    this.runSignal = context.signal;
    let resourceSet: Awaited<ReturnType<typeof externalResources>> | undefined;
    let modelGateway: Awaited<ReturnType<typeof openModelGateway>>;
    let server: Awaited<ReturnType<typeof startServer>> | undefined;
    let events: Promise<void> | undefined;
    let reconnects = 0;
    let unsupportedInteraction = false;
    const abort = () => {
      void this.interrupt();
    };
    context.signal.addEventListener('abort', abort);
    try {
      modelGateway = await openModelGateway(context.spec, context.signal);
      if (gate) resourceSet = await externalResources(plan.mcp, gate, context.signal);
      await observer.start(modelGateway?.endpoint);
      const instructions = [
        ...(Array.isArray(context.spec.instructions) ? (context.spec.instructions as string[]) : []),
        ...plan.instructions,
      ].join('\n\n');
      const instructionFile = join(state, 'instructions.md');
      writeFileSync(instructionFile, instructions, { mode: 0o600 });
      const toolPolicyFile = join(state, 'tool-policy.json');
      writeFileSync(toolPolicyFile, 'null', { mode: 0o600 });
      const turnStatePath = join(state, 'turns.json');
      writeFileSync(turnStatePath, '[]', { mode: 0o600 });
      const permission: Record<string, 'allow' | 'ask' | 'deny'> = {
        '*': plan.selection ? 'deny' : (config.permissionMode as 'allow'),
      };
      for (const [tool, enabled] of Object.entries(plan.selection ?? {}))
        if (enabled)
          permission[['write', 'edit', 'apply_patch'].includes(tool) ? 'edit' : tool] =
            config.permissionMode as 'allow';
      const native: Config = {
        $schema: 'https://opencode.ai/config.json',
        logLevel: 'ERROR',
        autoupdate: false,
        share: 'disabled',
        snapshot: false,
        formatter: false,
        lsp: false,
        enabled_providers: ['evalhive'],
        model: `evalhive/${model.name}`,
        small_model: `evalhive/${model.name}`,
        default_agent: 'build',
        provider: {
          evalhive: {
            name: 'EvalHive',
            npm: '@ai-sdk/openai-compatible',
            options: { apiKey: model.key, baseURL: observer.url },
            models: {
              [model.name]: {
                name: model.name,
                tool_call: true,
                temperature: true,
                reasoning: Boolean(config.reasoning),
                limit: { context: Number(config.contextWindow), output: Number(config.maxOutputTokens) },
                cost: { input: 0, output: 0, cache_read: 0, cache_write: 0 },
                headers: {
                  'User-Agent': 'evalhive/0.1 opencode/1.19.5',
                  'x-opencode-session': context.spec.attempt_id,
                },
              },
            },
          },
        },
        plugin: [
          [
            new URL('../native-hooks.mjs', import.meta.url).href,
            {
              externalSteps: Boolean(gate),
              toolPolicyPath: toolPolicyFile,
              turnStatePath,
              limitPath,
              ...(object(context.spec.limits).max_turns !== undefined
                ? { maxTurns: object(context.spec.limits).max_turns }
                : {}),
            },
          ],
        ],
        instructions: instructions ? [instructionFile] : [],
        skills: { paths: plan.skills },
        // Initialize native MCP processes one at a time within their own limits.
        // OpenCode otherwise starts every enabled server concurrently.
        mcp: Object.fromEntries(Object.entries(resourceSet?.mcp ?? plan.mcp).map(
          ([name, resource]) => [name, { ...resource, enabled: false }],
        )),
        permission,
        agent: {
          build: {
            ...(config.temperature !== undefined ? { temperature: Number(config.temperature) } : {}),
            ...(config.topP !== undefined ? { top_p: Number(config.topP) } : {}),
          },
          title: { disable: true },
          summary: { disable: true },
        },
        compaction: { auto: Boolean(config.autoCompaction) },
      };
      server = await startServer(state, context.spec.cwd, native);
      const nativeExited = new Promise<never>((_resolve, reject) => {
        server!.proc.once('exit', () => reject(new ProtocolError('SERVER_EXITED', 'OpenCode server exited during execution')));
      });
      nativeExited.catch(() => {});
      output.native('connector.stage', { stage: 'server_ready', pid: server.proc.pid ?? null });
      const client = createOpencodeClient({
        baseUrl: server.url,
        directory: context.spec.cwd,
        fetch: async (input, options) => {
          const request = input instanceof Request ? input : undefined;
          const method = options?.method ?? request?.method ?? 'GET';
          for (let retry = 0; ; retry++) {
            try { return await (this.transport.fetch ?? fetch)(input, options); }
            catch (error) {
              if (method === 'GET' && retry < 2 && error instanceof TypeError &&
                  !(options?.signal ?? request?.signal)?.aborted) {
                await delay(100 * (retry + 1));
                continue;
              }
            output.native('sdk.transport_failure', {
              path: new URL(request?.url ?? String(input)).pathname,
              method,
              name: error instanceof Error ? error.name : 'TransportError',
            });
            throw error;
            }
          }
        },
      });
      this.client = client;
      const created = await client.session.create(
        {
          title: context.spec.task.title ?? 'EvalHive execution',
          agent: 'build',
          model: { id: model.name, providerID: 'evalhive' },
        },
        { throwOnError: true, signal: AbortSignal.timeout(30000) },
      );
      const session = created.data!;
      output.native('connector.stage', { stage: 'session_created' });
      this.sessionId = session.id;
      const seed = nativeHistory(context.spec, session, model.name, resourceSet?.mapping ?? []);
      if (seed.messages.length) {
        const historyPath = join(state, 'history.json');
        writeFileSync(historyPath, JSON.stringify(seed), {mode: 0o600});
        await runFile(executable, ['import', historyPath], {cwd: context.spec.cwd, env: server.env, timeout: 30000, maxBuffer: 65536});
        // Imported messages are context, not newly observed execution or usage.
        for (const message of seed.messages) translator.imported.add(message.info.id);
      }
      if (gate) output.native('external_tools.mapping', {tools: resourceSet!.mapping, interface: 'adapted_mcp', history_messages: seed.messages.length});
      for (const [name, resource] of Object.entries(resourceSet?.mcp ?? plan.mcp)) {
        if (resource.enabled === false) continue;
        await client.mcp.connect(
          { name },
          { throwOnError: true, signal: AbortSignal.timeout(Number('timeout' in resource ? resource.timeout ?? 30000 : 30000) + 5000) },
        );
      }
      const statuses = await client.mcp.status(
        {},
        { throwOnError: true, signal: AbortSignal.timeout(30000) },
      );
      output.native('connector.stage', { stage: 'resources_ready' });
      output.native('mcp.status', { servers: Object.fromEntries(
        Object.entries(statuses.data ?? {}).map(([name, value]) => [name, object(value).status ?? 'unavailable']),
      ) });
      for (const [name, resource] of Object.entries(resourceSet?.mcp ?? plan.mcp)) {
        if (resource.enabled !== false && object(statuses.data?.[name]).status !== 'connected')
          throw new ProtocolError('MCP_UNAVAILABLE', 'A configured MCP server could not be initialized');
      }
      const toolIds = await client.tool.ids({}, { throwOnError: true, signal: this.apiSignal() });
      const tools = plan.selection
        ? Object.fromEntries(
            toolIds.data!.map((name) => [
              name,
              Object.entries(plan.selection!).some(
                ([pattern, enabled]) =>
                  enabled &&
                  (pattern === name || (pattern.endsWith('*') && name.startsWith(pattern.slice(0, -1)))),
              ),
            ]),
          )
        : undefined;
      writeFileSync(toolPolicyFile, JSON.stringify(tools ?? null), { mode: 0o600 });
      this.promptOptions = { model: { providerID: 'evalhive', modelID: model.name }, agent: 'build' };
      const publishPermission = (payload: Record<string, unknown>) => {
        const id = String(payload.id);
        if (this.permissions.has(id)) return;
        this.permissions.set(id, String(payload.sessionID));
        output.emit(
          'interaction.request',
          { request_id: id, kind: 'approval', payload: payload as JsonObject },
          String(payload.sessionID),
        );
      };
      let snapshotTail: Promise<unknown> = Promise.resolve();
      const history = () => {
        const current = snapshotTail.then(async () => {
          let main: Awaited<ReturnType<typeof client.session.messages>>['data'];
          for (const identity of [session.id, ...translator.parents.keys()]) {
            const response = await client.session.messages(
              { sessionID: identity },
              { throwOnError: true, signal: this.apiSignal() },
            );
            translator.snapshot(response.data!);
            if (identity === session.id) main = response.data;
          }
          const pending = await client.permission.list(
            {},
            { throwOnError: true, signal: this.apiSignal() },
          );
          for (const item of pending.data!) publishPermission(object(item));
          const questions = await client.question.list(
            {},
            { throwOnError: true, signal: this.apiSignal() },
          );
          if (questions.data!.length && !unsupportedInteraction) {
            unsupportedInteraction = true;
            await this.interrupt();
          }
          return main!;
        });
        snapshotTail = current.catch(() => {});
        return current;
      };
      let ready: (() => void) | undefined;
      const connected = new Promise<void>((resolve) => {
        ready = resolve;
      });
      events = (async () => {
        while (!streamAbort.signal.aborted) {
          const response = await client.event.subscribe(
            {},
            { signal: streamAbort.signal, sseMaxRetryAttempts: 1 },
          );
          for await (const event of response.stream) {
            if (streamAbort.signal.aborted) break;
            if (event.type === 'server.connected') {
              ready?.();
              continue;
            }
            const payload = object(event.properties);
            translator.event(event);
            if (event.type === 'permission.v2.asked' || event.type === 'permission.asked') {
              publishPermission(payload);
            } else if (event.type === 'permission.v2.replied' || event.type === 'permission.replied') {
              const id = String(payload.requestID ?? payload.id);
              if (this.permissions.delete(id))
                output.emit(
                  'interaction.resolved',
                  { request_id: id, kind: 'approval', payload: payload as JsonObject },
                  String(payload.sessionID),
                );
            } else if (event.type === 'question.v2.asked' || event.type === 'question.asked') {
              unsupportedInteraction = true;
              await this.interrupt();
              await client.question
                .reject({ requestID: String(payload.id) }, { throwOnError: true })
                .catch(() => {});
            }
          }
          if (!streamAbort.signal.aborted) {
            reconnects++;
            await history();
            await delay(100);
          }
        }
      })().catch(() => {
        if (!streamAbort.signal.aborted)
          output.fail('EVENT_STREAM_FAILED', 'OpenCode events could not be recovered');
      });
      let readyTimeout: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          connected,
          new Promise<never>((_, reject) => {
            readyTimeout = setTimeout(
              () =>
                reject(
                  new ProtocolError('EVENT_STREAM_UNAVAILABLE', 'OpenCode event stream did not become ready'),
                ),
              10000,
            );
          }),
        ]);
      } finally {
        if (readyTimeout) clearTimeout(readyTimeout);
      }
      if (context.signal.aborted) return { status: 'cancelled', summary: 'Run cancelled' };
      output.native('connector.stage', { stage: 'events_ready' });
      let last: Record<string, unknown> | undefined;
      let finalAnswer = '';
      for (;;) {
        const input = context.conversation ? await Promise.race([context.conversation.nextInput(), nativeExited]) : undefined;
        if (input === null) break;
        await this.sendPrompt(input ? (input.messages as JsonObject[]).map(item => String(item.content)).join('\n\n') : context.spec.task.prompt);
        output.native('connector.stage', { stage: 'prompt_admitted' });
        while (this.pendingFeedback.length) await this.sendPrompt(this.pendingFeedback.shift()!);
        this.started = true;
        for (;;) {
          const generation = this.feedbackGeneration;
          const messages = [...(await history())].sort(
            (a, b) => a.info.time.created - b.info.time.created || a.info.id.localeCompare(b.info.id),
          );
          const user = [...messages].reverse().find((item) => item.info.role === 'user');
          last = object([...messages].reverse().find((item) => item.info.role === 'assistant')?.info);
          const active = await client.session.status(
            {},
            { throwOnError: true, signal: this.apiSignal() },
          );
          const idle = !active.data?.[session.id] || active.data[session.id].type === 'idle';
          if (context.signal.aborted) {await this.interrupt(); break;}
          if (
            !this.controls &&
            generation === this.feedbackGeneration &&
            idle &&
            ((object(last.time).completed !== undefined && last.parentID === user?.info.id) ||
              translator.final.get(session.id)?.error)
          )
            break;
          if (server.proc.exitCode !== null || server.proc.signalCode !== null)
            throw new ProtocolError('SERVER_EXITED', 'OpenCode server exited during execution');
          await delay(200);
        }
        await batch;
        if (externalFailure) throw externalFailure;
        await output.drain();
        const completedMessages = await history();
        finalAnswer = completedMessages.filter(item => item.info.id === last?.id).flatMap(item => item.parts)
          .filter(part => part.type === 'text').map(part => String(object(part).text ?? '')).join('');
        if (!context.conversation || context.signal.aborted || existsSync(limitPath) || unsupportedInteraction ||
            !last || last.error || !['stop', 'end-turn'].includes(String(last.finish ?? ''))) break;
        await context.conversation.completeTurn(String(last.id), finalAnswer);
      }
      this.stopped = true;
      streamAbort.abort();
      await events;
      await history();
      await observer.close();
      await output.drain();
      if (context.signal.aborted) return { status: 'cancelled', summary: 'Run cancelled' };
      if (existsSync(limitPath))
        return {
          status: 'failed',
          summary: 'OpenCode reached the turn limit',
          error: {
            code: 'TURN_LIMIT',
            message: 'OpenCode reached the configured turn limit',
            details: { normalized_failure: { kind: 'limit' } },
          },
        };
      if (unsupportedInteraction)
        throw new ProtocolError('UNSUPPORTED_INTERACTION', 'OpenCode requested unsupported question input');
      const final = last && Object.keys(last).length ? last : translator.final.get(session.id);
      if ((!final && !context.conversation) || (final && (final.error || !['stop', 'end-turn'].includes(String(final.finish ?? '')))))
        return {
          status: 'failed',
          summary: 'OpenCode did not finish successfully',
          error: {
            code: 'OPENCODE_INCOMPLETE',
            message:
              typeof object(object(final?.error).data).message === 'string'
                ? String(object(object(final?.error).data).message)
                : 'OpenCode ended without a successful final answer',
            details: {
              normalized_failure:
                observer.lastStatus && observer.lastStatus >= 400
                  ? { kind: 'model_error', status_code: observer.lastStatus }
                  : { kind: final?.finish === 'length' ? 'limit' : 'harness_error' },
            },
          },
        };
      return {
        status: 'success',
        summary: finalAnswer || 'OpenCode completed',
        metadata: {
          harness_version: '1.19.5',
          adapter_version: ADAPTER_VERSION,
          effective_config: config,
          session_id: session.id,
          recording: {
            native_events: 'observed_stream_and_authoritative_snapshots',
            ...(reconnects ? { missing_stream_deltas: 'possible' } : {}),
            reconnects,
            usage: 'provider_reported_or_unknown',
            seeded_history_messages: conversationHistory(context.spec).length,
          },
        },
      };
    } catch (error) {
      if (context.signal.aborted) return { status: 'cancelled', summary: 'Run cancelled' };
      output.native('connector.failure', {
        name: error instanceof Error ? error.name : 'SdkError',
        message: String(error instanceof Error ? error.message : JSON.stringify(error)).slice(0, 4096),
        native_stderr: (server?.diagnostics() ?? String(object(error).nativeStderr ?? '')).slice(-16384),
      });
      await output.drain().catch(() => {});
      const failure = output.error ?? error;
      return {
        status: 'failed',
        summary: failure instanceof ProtocolError ? failure.message : 'OpenCode execution failed',
        error: {
          code: failure instanceof ProtocolError ? failure.code : 'OPENCODE_FAILED',
          message:
            failure instanceof ProtocolError ? failure.message : 'OpenCode server or SDK request failed',
          details: { normalized_failure: { kind: output.error ? 'recording_error' : 'harness_error' } },
        },
      };
    } finally {
      this.stopped = true;
      context.signal.removeEventListener('abort', abort);
      streamAbort.abort();
      let cleanupFailed = false;
      const cleanup = async (stage: string, operation: () => Promise<unknown> | void) => {
        try { await operation(); }
        catch (error) {
          cleanupFailed = true;
          const code = object(error).code;
          output.native('connector.cleanup_failure', {
            stage,
            name: error instanceof Error ? error.name.slice(0, 80) : 'Error',
            code: typeof code === 'string' && /^[A-Z_]{1,64}$/.test(code) ? code : 'UNKNOWN',
          });
        }
      };
      await cleanup('event_stream', async () => { await events; });
      await cleanup('session_abort', () => this.interrupt());
      // Close native MCP clients while the server can still supervise their children.
      if (this.client) await Promise.allSettled(Object.keys(plan.mcp).map(name =>
        this.client!.mcp.disconnect({name}, {throwOnError: true, signal: AbortSignal.timeout(5000)})));
      await cleanup('server', async () => { if (server) await stopServer(server.proc); });
      await cleanup('observer', () => observer.close());
      await cleanup('external_resources', async () => { await resourceSet?.close(); });
      await cleanup('model_gateway', async () => { await modelGateway?.close(); });
      this.client = undefined;
      this.sessionId = undefined;
      await cleanup('state_directory', () => rmSync(state, {
        recursive: true, force: true, maxRetries: 5, retryDelay: 100,
      }));
      await output.drain().catch(() => {});
      if (cleanupFailed) throw new ProtocolError('OPENCODE_CLEANUP_FAILED', 'OpenCode cleanup did not complete');
    }
  }

  async control(context: RunContext, name: string, payload: JsonObject): Promise<JsonValue> {
    if (context.conversation && name === 'feedback')
      throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Conversation input requires turn.submit');
    if (this.stopped) throw new ProtocolError('ATTEMPT_NOT_ACTIVE', 'OpenCode has already stopped');
    if (name === 'abort') {
      context.abort.abort();
      await this.interrupt();
      return { accepted: true };
    }
    if (name === 'feedback' && !this.started) {
      if (this.pendingFeedback.length >= 32)
        throw new ProtocolError('FEEDBACK_QUEUE_FULL', 'OpenCode feedback queue is full');
      this.pendingFeedback.push(String(payload.text));
      return { accepted: true, disposition: 'queued' };
    }
    if (!this.client || !this.sessionId)
      throw new ProtocolError('ATTEMPT_NOT_READY', 'OpenCode session is not ready');
    this.controls++;
    try {
      if (name === 'feedback') {
        this.feedbackGeneration++;
        await this.sendPrompt(String(payload.text));
        return { accepted: true, disposition: 'queued' };
      }
      if (name === 'approval') {
        const sessionID = this.permissions.get(String(payload.request_id));
        if (!sessionID)
          throw new ProtocolError('INTERACTION_NOT_FOUND', 'Permission request is no longer active');
        await this.client.permission.reply(
          { requestID: String(payload.request_id), reply: payload.decision as 'once' | 'always' | 'reject' },
          { throwOnError: true },
        );
        return { accepted: true };
      }
      throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'OpenCode command is unavailable');
    } finally {
      this.controls--;
      this.feedbackGeneration++;
    }
  }
}
