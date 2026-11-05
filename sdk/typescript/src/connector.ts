import { randomUUID } from 'node:crypto';
import { once } from 'node:events';
import { stdin, stdout } from 'node:process';
import {Conversation, CONVERSATION_COMMANDS, CONVERSATION_EVENTS} from './conversation.js';
import {
  PROTOCOL_VERSION, ProtocolError, decodeLine, encodeLine, errorResponse,
  lines, response, validate, validateRun,
  type JsonObject, type JsonValue, type Manifest, type RunSpec, type RunResult, type HarnessEvent, type WireMessage,
} from './protocol.js';

export class RunContext {
  private seq = 0;
  private finished = false;
  private output: Promise<void> = Promise.resolve();
  readonly abort = new AbortController();
  readonly conversation: Conversation | undefined;
  get signal(): AbortSignal { return this.abort.signal; }
  constructor(readonly spec: RunSpec, private send: (value: WireMessage) => Promise<void>) {
    if ((spec.interaction as JsonObject | undefined)?.mode && (spec.interaction as JsonObject).mode !== 'single_turn')
      this.conversation = new Conversation(this);
  }
  emit(event: string, data: JsonObject, extra: {source?: string; session_id?: string; parent_session_id?: string} = {}): Promise<void> {
    if (['run.started', 'run.finished'].includes(event) || CONVERSATION_EVENTS.has(event))
      return Promise.reject(new ProtocolError('RESERVED_EVENT', 'Run lifecycle is owned by the SDK'));
    return this.lifecycle(event, data, extra);
  }
  /** SDK lifecycle dispatcher; connectors call emit() instead. */
  lifecycle(event: string, data: JsonObject, extra: {source?: string; session_id?: string; parent_session_id?: string} = {}): Promise<void> {
    const next = this.output.then(async () => {
      if (this.finished) throw new ProtocolError('ATTEMPT_FINISHED', 'Attempt has finished');
      const value = validate<HarnessEvent>('event', {protocol_version: PROTOCOL_VERSION, type: 'event',
        event_id: randomUUID(), attempt_id: this.spec.attempt_id, seq: this.seq,
        source: 'main', ...extra, event, data});
      await this.send(value); this.seq++;
      if (event === 'run.finished') this.finished = true;
    });
    // A rejected event should not poison the ordering lock for later diagnostics.
    this.output = next.catch(() => {});
    return next;
  }
}

export interface Connector {
  manifest: Manifest;
  validate?(spec: RunSpec): Promise<JsonValue>;
  run(context: RunContext): Promise<RunResult>;
  control?(context: RunContext, name: string, payload: JsonObject): Promise<JsonValue>;
}

export async function serve(connector: Connector, input: AsyncIterable<Uint8Array>, write: (data: Buffer) => Promise<void>): Promise<void> {
  validate('manifest', connector.manifest);
  let output = Promise.resolve();
  const send = (value: WireMessage) => {
    const data = encodeLine(value);
    const next = output.then(() => write(data)); output = next.catch(() => {}); return next;
  };
  let context: RunContext | undefined;
  let work: Promise<void> | undefined;
  let active = false;
  let started = false;
  const stop = async () => {
    context?.abort.abort();
    if (work) await work;
  };
  try {
    for await (const line of lines(input)) {
      let id: string | null = null;
      try {
        const message = decodeLine(line);
        if (message.type !== 'command') throw new ProtocolError('INVALID_MESSAGE', 'Expected a command');
        id = message.id;
        const {method, params} = message;
        let result: JsonValue;
        if (method === 'describe') result = connector.manifest;
        else if (method === 'validate' || method === 'start') {
          const spec = validateRun(connector.manifest, params.run as RunSpec);
          if (method === 'start' && started) throw new ProtocolError('ALREADY_STARTED', 'One process executes one attempt');
          result = connector.validate ? await connector.validate(spec) : {accepted: true, effective_config: spec.config};
          validate('validation_result', result);
          if (method === 'start') {
            const ctx = context = new RunContext(spec, send);
            started = active = true;
            await send(response(id, {accepted: true}));
            work = (async () => {
              await ctx.lifecycle('run.started', {});
              let terminal: RunResult;
              try {
                await ctx.conversation?.ready();
                terminal = validate('result', await connector.run(ctx));
                if (ctx.conversation && !ctx.signal.aborted && terminal.status === 'success' && ctx.conversation.state !== 'closed')
                  throw new ProtocolError('CONVERSATION_NOT_CLOSED', 'Connector returned before explicit conversation close');
                if (ctx.signal.aborted) terminal = {status: 'cancelled', summary: 'Run cancelled'};
              }
              catch (error) {
                terminal = ctx.signal.aborted ? {status: 'cancelled', summary: 'Run cancelled'} :
                  {status: 'failed', summary: 'Connector failed', error: error instanceof ProtocolError ? error.asData() :
                    {code: 'CONNECTOR_ERROR', message: 'Connector execution failed'}};
              }
              await ctx.lifecycle('run.finished', terminal); active = false;
            })();
            // Observe failures now even if the host has not sent shutdown yet.
            work.catch(() => {});
            continue;
          }
        } else if (method === 'control') {
          if (!active || !context || params.attempt_id !== context.spec.attempt_id)
            throw new ProtocolError('ATTEMPT_NOT_ACTIVE', 'Attempt is not active');
          const name = params.command as string;
          if (!['native', 'emulated'].includes(connector.manifest.capabilities[name] ?? 'unsupported') ||
              (!Object.hasOwn(CONVERSATION_COMMANDS, name) && !connector.control))
            throw new ProtocolError('UNSUPPORTED_CAPABILITY', `Unavailable command: ${name}`);
          const payload = (params.payload ?? {}) as JsonObject;
          const commandSchema = connector.manifest.commands?.[name];
          if (commandSchema) validateRun({...connector.manifest, config_schema: commandSchema.input_schema},
            {...context.spec, config: payload, required_capabilities: []});
          if (Object.hasOwn(CONVERSATION_COMMANDS, name)) {
            if (!context.conversation) throw new ProtocolError('UNSUPPORTED_INTERACTION', 'Persistent conversation was not selected');
            result = await context.conversation.control(name, payload);
          } else {
            if (name === 'abort') context.abort.abort();
            result = await connector.control!(context, name, payload);
          }
        } else {
          await stop(); await send(response(id, {stopped: true})); break;
        }
        await send(response(id, result));
      } catch (error) {
        await send(errorResponse(id, error instanceof ProtocolError ? error :
          new ProtocolError('CONNECTOR_ERROR', 'Command failed')));
      }
    }
  } catch (error) {
    if (error instanceof ProtocolError) await send(errorResponse(null, error));
    else throw error;
  } finally { await stop(); }
}

export async function serveStdio(connector: Connector): Promise<void> {
  await serve(connector, stdin, async data => {
    if (!stdout.write(data)) await once(stdout, 'drain');
  });
}
