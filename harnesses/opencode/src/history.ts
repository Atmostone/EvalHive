import {conversationHistory, mapHistoryTools, type JsonObject, type RunSpec} from '@evalhive/harness-sdk';
import type {Session, Message, Part, ToolPart} from '@opencode-ai/sdk/v2';

export function nativeHistory(spec: RunSpec, session: Session, model: string, mapping: JsonObject[]): {info: Session; messages: {info: Message; parts: Part[]}[]} {
  const messages: {info: Message; parts: Part[]}[] = [];
  const calls = new Map<string, ToolPart>();
  let parentID = 'msg_000000000000000000seedroot';
  let partIndex = 0;
  for (const [index, item] of mapHistoryTools(conversationHistory(spec), mapping).entries()) {
    const timestamp = Date.now() - 100000 + index;
    if (item.role === 'tool') {
      const part = calls.get(String(item.call_id))!;
      part.state = item.is_error ? {status: 'error', input: part.state.input, error: String(item.content),
        time: {start: timestamp, end: timestamp}} : {status: 'completed', input: part.state.input,
        title: '', output: String(item.content), metadata: {imported_history: true}, time: {start: timestamp, end: timestamp}};
      continue;
    }
    const id = `msg_000000000000000000seed${String(index).padStart(6, '0')}`;
    const entry: {info: Message; parts: Part[]} = {info: item.role === 'user' ? {
      id, sessionID: session.id, role: 'user', time: {created: timestamp}, agent: 'build',
      model: {providerID: 'evalhive', modelID: model},
    } : {id, sessionID: session.id, role: 'assistant', parentID, agent: 'build', mode: 'build',
      modelID: model, providerID: 'evalhive', path: {cwd: spec.cwd, root: spec.cwd},
      time: {created: timestamp, completed: timestamp}, finish: item.tool_calls ? 'tool-calls' : 'stop',
      cost: 0, tokens: {input: 0, output: 0, reasoning: 0, cache: {read: 0, write: 0}}}, parts: []};
    const partId = () => `prt_000000000000000000seed${String(++partIndex).padStart(6, '0')}`;
    if (item.content) entry.parts.push({id: partId(), sessionID: session.id, messageID: id, type: 'text', text: String(item.content)});
    for (const call of (item.tool_calls ?? []) as {call_id: string; name: string; arguments: Record<string, unknown>}[]) {
      const part: ToolPart = {id: partId(), sessionID: session.id, messageID: id, type: 'tool', callID: call.call_id,
        tool: call.name, state: {status: 'pending', input: call.arguments, raw: JSON.stringify(call.arguments)}};
      calls.set(call.call_id, part); entry.parts.push(part);
    }
    if (item.role === 'user') parentID = id;
    messages.push(entry);
  }
  return {info: session, messages};
}
