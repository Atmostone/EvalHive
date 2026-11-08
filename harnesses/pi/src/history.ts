import type {Message} from '@earendil-works/pi-ai';
import {conversationHistory, mapHistoryTools, type JsonObject, type RunSpec} from '@evalhive/harness-sdk';

export function nativeHistory(spec: RunSpec, model: string, mapping: JsonObject[]): Message[] {
  const names = new Map<string, string>();
  return mapHistoryTools(conversationHistory(spec), mapping).map((item, index): Message => {
    const timestamp = Date.now() - 100000 + index;
    if (item.role === 'user') return {role: 'user', content: String(item.content), timestamp};
    if (item.role === 'tool') return {role: 'toolResult', toolCallId: String(item.call_id),
      toolName: names.get(String(item.call_id))!, content: [{type: 'text', text: String(item.content)}],
      isError: Boolean(item.is_error), timestamp};
    const calls = (item.tool_calls ?? []) as {call_id: string; name: string; arguments: JsonObject}[];
    calls.forEach(call => names.set(call.call_id, call.name));
    return {role: 'assistant', api: 'openai-completions', provider: 'evalhive', model, timestamp,
      content: [{type: 'text', text: String(item.content)}, ...calls.map(call => ({type: 'toolCall' as const,
        id: call.call_id, name: call.name, arguments: call.arguments}))], stopReason: calls.length ? 'toolUse' : 'stop',
      // Imported history has no observed provider request. These native fields
      // are required for replay and are never projected as current-run usage.
      usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}}};
  });
}
