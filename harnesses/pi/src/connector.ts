import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager, VERSION,
  type AgentSession, type AgentSessionEvent, type ExtensionFactory,
} from '@earendil-works/pi-coding-agent';
import { ProtocolError, ExternalToolGate, conversationHistory, type Connector, type JsonObject, type JsonValue, type RunContext, type RunResult, type RunSpec } from '@evalhive/harness-sdk';
import { ADAPTER_VERSION, connection, effectiveConfig, manifest, object } from './config.js';
import { observeRequests } from './transport.js';
import { EventQueue, json, UsageTracker } from './events.js';
import { failureFacts } from './errors.js';
import { connectResources, resourcePlan } from './resources.js';
import {nativeHistory} from './history.js';

function text(content: unknown, kind: string): string {
  return Array.isArray(content) ? content.map(object).filter((part) => part.type === kind)
    .map((part) => typeof part[kind] === 'string' ? part[kind] as string : '').join('') : '';
}

export class PiConnector implements Connector {
  readonly manifest = manifest;
  private session: AgentSession | undefined;
  private settled = false;
  private pendingFeedback: string[] = [];

  constructor(private credentials: Record<string, string>) {}

  async validate(spec: RunSpec): Promise<JsonValue> {
    if (VERSION !== '1.5.0') throw new ProtocolError('NATIVE_VERSION_MISMATCH', 'Installed Pi version does not match the connector');
    const config = effectiveConfig(spec);
    connection(spec, this.credentials);
    resourcePlan(spec, this.credentials);
    conversationHistory(spec);
    if (Number(config.maxOutputTokens) > Number(config.contextWindow))
      throw new ProtocolError('INVALID_LIMITS', 'Output limit cannot exceed the context window');
    if (config.thinkingLevel !== 'off' && !config.reasoning)
      throw new ProtocolError('INVALID_THINKING', 'Enable the model reasoning capability before selecting a thinking level');
    return {accepted: true, effective_config: config};
  }

  async run(context: RunContext): Promise<RunResult> {
    const config = effectiveConfig(context.spec);
    const model = connection(context.spec, this.credentials);
    const state = mkdtempSync(join(tmpdir(), 'evalhive-pi-'));
    mkdirSync(join(state, 'sessions'), {mode: 0o700});
    let session: AgentSession | undefined;
    let unsubscribe: (() => void) | undefined;
    let queue: EventQueue | undefined;
    let budgetHit = false;
    let turns = 0;
    let messageIndex = 0;
    let assistantId = '';
    let userId = '';
    let compactionActive = false;
    let agentStarted = false;
    let responseStatus: number | undefined;
    let resourceSet: Awaited<ReturnType<typeof connectResources>> | undefined;
    let complete: (() => void) | undefined;
    let settled: Promise<void>;
    let externalFailure: unknown;
    let batch: Promise<unknown> = Promise.resolve();
    const gate = context.conversation?.mode === 'external_steps' ? new ExternalToolGate(context.conversation, context.signal) : undefined;
    const abort = () => { void session?.abort().catch(() => {}); };
    context.signal.addEventListener('abort', abort);
    try {
      resourceSet = await connectResources(context.spec, this.credentials, context.signal, gate,
        status => context.emit('native', {name: 'mcp.status', payload: status, adapter_version: manifest.version}, {source: 'pi'}));
      const runtime = await ModelRuntime.create({authPath: join(state, 'auth.json'), modelsPath: null,
        modelsStorePath: join(state, 'models.sqlite'), allowModelNetwork: false, refreshOnCreate: false});
      runtime.registerProvider('evalhive', {
        api: 'openai-completions', baseUrl: model.endpoint,
        headers: {'User-Agent': 'evalhive/0.1', 'x-opencode-session': context.spec.attempt_id},
        models: [{id: model.name, name: model.name, reasoning: Boolean(config.reasoning), input: ['text'],
          contextWindow: Number(config.contextWindow), maxTokens: Number(config.maxOutputTokens),
          cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0},
          compat: {...object(config.compat), supportsUsageInStreaming: object(config.compat).supportsUsageInStreaming !== false},
        }],
      });
      await runtime.setRuntimeApiKey('evalhive', model.key);
      const selected = runtime.getModel('evalhive', model.name);
      if (!selected) throw new ProtocolError('MODEL_UNAVAILABLE', 'Configured Pi model is unavailable');
      const sessions = SessionManager.create(context.spec.cwd, join(state, 'sessions'));
      queue = new EventQueue(context, sessions.getSessionId(), abort);
      const output = queue;
      const usage = new UsageTracker(output, model.name);
      const finishObservation = observeRequests(runtime, usage, output,
        () => compactionActive ? 'compaction' : 'agent', value => { responseStatus = value; });
      const bridge: ExtensionFactory = (api) => {
        api.on('before_provider_request', async (event) => {
          await batch;
          if (externalFailure) throw externalFailure;
          const payload = object(event.payload);
          return {...payload,
            ...(config.temperature !== undefined ? {temperature: config.temperature} : {}),
            ...(config.seed !== undefined ? {seed: config.seed} : {}),
          };
        });
      };
      const settings = SettingsManager.inMemory({compaction: {...object(config.compaction), enabled: Boolean(config.autoCompaction)},
        cacheWarming: 'off', retry: {enabled: Number(config.maxRetries) > 0,
          maxRetries: Number(config.maxRetries), baseDelayMs: Number(config.retryBaseDelayMs),
          provider: {maxRetries: 0, timeoutMs: 60000}}, packages: []});
      const loader = new DefaultResourceLoader({cwd: context.spec.cwd, agentDir: state, settingsManager: settings,
        noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
        skillsOverride: () => resourceSet!.loadSkills(state),
        extensionFactories: [bridge], appendSystemPrompt: [...(Array.isArray(context.spec.instructions) ?
          context.spec.instructions.filter((value): value is string => typeof value === 'string') : []), ...resourceSet.instructions]});
      await loader.reload();
      ({session} = await createAgentSession({cwd: context.spec.cwd, agentDir: state, modelRuntime: runtime,
        model: selected, thinkingLevel: config.thinkingLevel as 'off', sessionManager: sessions,
        settingsManager: settings, resourceLoader: loader, tools: resourceSet.tools, customTools: resourceSet.customTools}));
      this.session = session;
      const current = session;
      const seed = nativeHistory(context.spec, model.name, resourceSet.mapping);
      for (const message of seed) sessions.appendMessage(message);
      current.agent.state.messages = [...current.agent.state.messages, ...seed];
      if (gate) output.native('external_tools.mapping', {tools: resourceSet.mapping, interface: 'adapted_mcp', history_messages: seed.length});
      if (current.thinkingLevel !== config.thinkingLevel)
        throw new ProtocolError('UNSUPPORTED_THINKING', 'The configured model does not support this thinking level');
      unsubscribe = current.subscribe((event: AgentSessionEvent) => {
        output.native(event.type, event);
        if (event.type === 'agent_start') {
          agentStarted = true;
          for (const feedback of this.pendingFeedback.splice(0))
            void current.steer(feedback).catch(() => abort());
        } else if (event.type === 'turn_start') {
          turns++;
          if (turns > Number(object(context.spec.limits).max_turns ?? Infinity)) { budgetHit = true; abort(); }
        } else if (event.type === 'message_start') {
          const message = object(event.message);
          if (message.role === 'assistant') assistantId = `message-${++messageIndex}`;
          if (message.role === 'user') userId = `message-${++messageIndex}`;
        } else if (event.type === 'message_update') {
          const update = object(event.assistantMessageEvent);
          if (update.type === 'text_delta' || update.type === 'thinking_delta')
            output.emit('message.delta', {message_id: assistantId, role: 'assistant',
              channel: update.type === 'thinking_delta' ? 'reasoning' : 'text', delta: String(update.delta ?? '')});
        } else if (event.type === 'message_end') {
          const message = object(event.message);
          if (gate && message.role === 'assistant' && message.stopReason === 'toolUse') {
            const calls = (Array.isArray(message.content) ? message.content : []).map(object).filter(part => part.type === 'toolCall')
              .map(part => ({call_id: String(part.id), name: String(part.name), arguments: json(part.arguments) as JsonObject}));
            if (calls.length) {
              try {batch = gate.announce(assistantId, assistantId, calls, text(message.content, 'text'));}
              catch (error) {externalFailure = error; abort();}
              batch = batch.catch(error => {externalFailure = error; abort();});
            }
          }
          if (message.role === 'assistant' || message.role === 'user') {
            const id = message.role === 'assistant' ? assistantId : userId;
            output.emit('message.end', {message_id: id || `message-${++messageIndex}`, role: message.role,
              content: typeof message.content === 'string' ? message.content : text(message.content, 'text'),
              ...(message.role === 'assistant' ? {stop_reason: String(message.stopReason ?? 'unknown')} : {})});
            const reasoning = text(message.content, 'thinking');
            if (reasoning) output.emit('message.end', {message_id: id, role: 'assistant', channel: 'reasoning', content: reasoning});
          }
        } else if (event.type === 'tool_execution_start') {
          output.emit('tool.start', {call_id: event.toolCallId, name: event.toolName,
            ...(resourceSet?.aliases[event.toolName]?.length ? {aliases: resourceSet.aliases[event.toolName]} : {}),
            arguments: json(event.args) as JsonObject, ...(assistantId ? {message_id: assistantId} : {}),
            ...(event.parentToolCallId ? {parent_call_id: event.parentToolCallId} : {})});
        } else if (event.type === 'tool_execution_update') {
          output.emit('tool.update', {call_id: event.toolCallId, mode: 'snapshot', content: json(event.partialResult),
            ...(event.parentToolCallId ? {parent_call_id: event.parentToolCallId} : {})});
        } else if (event.type === 'tool_execution_end') {
          output.emit('tool.end', {call_id: event.toolCallId, status: event.isError ? 'error' : 'completed',
            result: json(event.result), ...(event.durationMs !== undefined ? {duration_ms: event.durationMs} : {}),
            ...(event.parentToolCallId ? {parent_call_id: event.parentToolCallId} : {})});
        } else if (event.type === 'compaction_start') {
          compactionActive = true;
        } else if (event.type === 'compaction_end') {
          compactionActive = false;
        } else if (event.type === 'agent_settled') {
          this.settled = !context.conversation; complete?.();
        }
      });
      await current.bindExtensions({});
      if (context.signal.aborted) return {status: 'cancelled', summary: 'Run cancelled'};
      for (;;) {
        const input = context.conversation ? await context.conversation.nextInput() : undefined;
        if (input === null) break;
        const prompt = input ? (input.messages as JsonObject[]).map(item => String(item.content)).join('\n\n') : context.spec.task.prompt;
        agentStarted = false;
        settled = new Promise<void>(resolve => {complete = resolve;});
        await current.prompt(prompt, {expandPromptTemplates: false, source: 'rpc'});
        if (!agentStarted && !context.signal.aborted)
          throw new ProtocolError('PROMPT_NOT_EXECUTED', 'Pi did not start an agent run');
        if (agentStarted) await settled;
        await batch;
        if (externalFailure) throw externalFailure;
        await output.drain();
        if (context.signal.aborted || budgetHit || !context.conversation) break;
        const last = [...current.messages].reverse().map(object).find(message => message.role === 'assistant');
        if (!last || last.stopReason !== 'stop') break;
        await context.conversation.completeTurn(assistantId, text(last.content, 'text'));
      }
      await finishObservation();
      await output.drain();
      if (context.signal.aborted) return {status: 'cancelled', summary: 'Run cancelled'};
      if (budgetHit) return {status: 'failed', summary: 'Pi reached the turn limit',
        error: {code: 'TURN_LIMIT', message: 'Pi reached the configured turn limit', details: {normalized_failure: {kind: 'limit'}}}};
      const last = [...current.messages].reverse().map(object).find((message) => message.role === 'assistant');
      const metadata = {harness_version: VERSION, adapter_version: ADAPTER_VERSION, session_id: sessions.getSessionId(),
        effective_config: config, recording: {native_events: 'observed_with_bounded_request_snapshots', usage: 'reported_or_unknown',
          seeded_history_messages: seed.length}};
      if ((!last && !context.conversation) || (last && last.stopReason !== 'stop')) return {status: 'failed', summary: 'Pi did not finish successfully',
        error: {code: 'PI_INCOMPLETE', message: typeof last?.errorMessage === 'string' ? last.errorMessage : 'Pi ended without a successful final answer',
          details: {normalized_failure: last ? failureFacts(last.stopReason, last.errorMessage, responseStatus) : {kind: 'harness_error'}}}, metadata};
      return {status: 'success', summary: text(last?.content, 'text') || 'Pi completed', metadata};
    } catch (error) {
      if (context.signal.aborted) return {status: 'cancelled', summary: 'Run cancelled'};
      if (error instanceof ProtocolError) return {status: 'failed', summary: error.message,
        error: {...error.asData(), details: {normalized_failure: {kind: queue?.error ? 'recording_error' : 'harness_error'}}}};
      throw error;
    } finally {
      context.signal.removeEventListener('abort', abort);
      unsubscribe?.();
      session?.dispose();
      await resourceSet?.close();
      this.session = undefined;
      rmSync(state, {recursive: true, force: true});
    }
  }

  async control(context: RunContext, name: string, payload: JsonObject): Promise<JsonValue> {
    if (this.settled) throw new ProtocolError('ATTEMPT_NOT_ACTIVE', 'Pi has already settled');
    if (name === 'abort') {
      context.abort.abort();
      await this.session?.abort();
      return {accepted: true};
    }
    if (name === 'feedback') {
      if (context.conversation) throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Conversation input requires turn.submit');
      if (typeof payload.text !== 'string' || !payload.text.trim()) throw new ProtocolError('INVALID_FEEDBACK', 'Feedback must contain text');
      if (!this.session) {
        if (this.pendingFeedback.length >= 32) throw new ProtocolError('FEEDBACK_QUEUE_FULL', 'Pi feedback queue is full');
        this.pendingFeedback.push(payload.text);
        return {accepted: true, disposition: 'queued'};
      }
      return {accepted: true, disposition: await this.session.steer(payload.text)};
    }
    throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Pi command is unavailable');
  }
}
