import type { JsonObject } from '@evalhive/harness-sdk';

// Pi 1.5.0 formats OpenAI-compatible errors as "<status>: <body>" or
// the SDK's own "<status> <message>". Keep that native parsing in this package.
export function failureFacts(stopReason: unknown, message: unknown, responseStatus?: number): JsonObject {
  if (stopReason === 'length') return {kind: 'limit'};
  if (stopReason !== 'error') return {kind: 'agent_error'};
  const text = typeof message === 'string' ? message : '';
  if (/Stream ended without finish_reason|premature close/i.test(text)) return {kind: 'recording_error'};
  const parsed = text.match(/^([1-5][0-9]{2})(?::|\s)/);
  const status = parsed ? Number(parsed[1]) : responseStatus ?? null;
  let exception: string | null = null;
  if (/Connection error|fetch failed|ENOTFOUND|ECONNREFUSED|EAI_AGAIN/i.test(text)) exception = 'APIConnectionError';
  if (/timed out|timeout/i.test(text) && status === null) exception = 'APITimeoutError';
  return {kind: 'model_error', status_code: status, exception};
}
