import { createHash } from 'node:crypto';
import type { ModelRuntime } from '@earendil-works/pi-coding-agent';
import { object } from './config.js';
import type { EventQueue, UsageTracker } from './events.js';
import { failureFacts } from './errors.js';

/** Observe every native request, including summaries that bypass agent hooks. */
export function observeRequests(runtime: ModelRuntime, usage: UsageTracker, output: EventQueue,
  purpose: () => string, status: (value: number) => void) {
  const native = runtime.streamSimple.bind(runtime);
  const pending = new Set<Promise<void>>();
  runtime.streamSimple = (model, context, options = {}) => {
    let identity: string | undefined;
    let responseStatus: number | undefined;
    let failureRecorded = false;
    const requestPurpose = purpose();
    const failure = (facts: ReturnType<typeof failureFacts>) => {
      if (!identity || failureRecorded) return;
      failureRecorded = true;
      output.emit('failure.observed', {request_id: identity, purpose: requestPurpose, facts});
    };
    const stream = native(model, context, {
      ...options,
      onPayload: async (payload, physicalModel) => {
        const replacement = await options.onPayload?.(payload, physicalModel);
        const applied = replacement === undefined ? payload : replacement;
        identity = usage.begin(requestPurpose);
        const serialized = JSON.stringify(applied);
        const bytes = Buffer.byteLength(serialized);
        output.native('provider.request', {request_id: identity, purpose: requestPurpose,
          requested_model: physicalModel.id, bytes,
          sha256: createHash('sha256').update(serialized).digest('hex'),
          ...(bytes <= 256 * 1024 ? {payload: applied} : {payload_omitted: 'request_exceeds_native_snapshot_limit'})});
        return replacement;
      },
      onResponse: async (response, physicalModel) => {
        responseStatus = response.status;
        status(response.status);
        if (response.status >= 400) failure(failureFacts('error', '', response.status));
        output.native('provider.response', {request_id: identity, purpose: requestPurpose,
          requested_model: physicalModel.id, status: response.status});
        await options.onResponse?.(response, physicalModel);
      },
      onProviderStreamEvent: async (data, physicalModel) => {
        output.native('provider_stream_event', {data, model: physicalModel.id, provider: physicalModel.provider,
          api: physicalModel.api, request_id: identity, purpose: requestPurpose});
        usage.provider(data, physicalModel.id, identity);
        await options.onProviderStreamEvent?.(data, physicalModel);
      },
    });
    const complete = stream.result().then(message => {
      if (message.stopReason === 'error') failure(failureFacts(message.stopReason, message.errorMessage, responseStatus));
      if (identity) usage.assistant(object(message).usage, identity);
    }).catch(() => { failure({kind: 'model_error', exception: 'APIConnectionError', status_code: responseStatus ?? null}); });
    pending.add(complete);
    void complete.finally(() => pending.delete(complete));
    return stream;
  };
  return async () => { await Promise.all([...pending]); };
}
