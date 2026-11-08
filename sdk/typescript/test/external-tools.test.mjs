import assert from 'node:assert/strict';
import test from 'node:test';
import {ExternalToolGate, conversationHistory, contentHash, mapHistoryTools} from '../dist/index.js';

test('history requires complete causally paired tool batches', () => {
  const call = {call_id: 'a', name: 'lookup', arguments: {query: 'x'}};
  const assistant = {role: 'assistant', content: '', tool_calls: [call]};
  const tool = {role: 'tool', content: 'result', call_id: 'a'};
  const parse = history => conversationHistory({interaction: {initial_history: history}});
  assert.deepEqual(parse([assistant, tool]), [assistant, tool]);
  for (const history of [[tool], [assistant], [assistant, {role: 'user', content: 'new'}],
    [assistant, tool, assistant, tool], [assistant, tool, tool]])
    assert.throws(() => parse(history), error => error.code === 'INVALID_HISTORY');
  assert.equal(contentHash({b: 2, a: 1}), contentHash({a: 1, b: 2}));
});

test('history uses exposed tool names and rejects ambiguous native names', () => {
  const history = [{role: 'assistant', content: '', tool_calls: [{call_id: 'old', name: 'mcp__domain__lookup', arguments: {}}]}];
  const mapping = [{native_name: 'lookup', canonical_name: 'mcp__domain__lookup', exposed_name: 'domain_lookup'}];
  assert.equal(mapHistoryTools(history, mapping)[0].tool_calls[0].name, 'domain_lookup');
  assert.equal(history[0].tool_calls[0].name, 'mcp__domain__lookup');
  assert.throws(() => mapHistoryTools([{...history[0], tool_calls: [{...history[0].tool_calls[0], name: 'lookup'}]}],
    [...mapping, {native_name: 'lookup', canonical_name: 'mcp__other__lookup', exposed_name: 'other_lookup'}]), error => error.code === 'INVALID_HISTORY');
});

test('native gate matches calls by ID and content, including execution before batch observation', async () => {
  let finish;
  const controller = new AbortController();
  const gate = new ExternalToolGate({requestTools: () => new Promise(resolve => {finish = resolve;})}, controller.signal);
  const a = {call_id: 'a', name: 'lookup', arguments: {query: 'x'}}, b = {...a, call_id: 'b'};
  const early = gate.execute('b', 'lookup', {query: 'x'});
  const batch = gate.announce('message', 'batch', [a, b]);
  await assert.rejects(gate.execute('a', 'lookup', {query: 'changed'}), error => error.code === 'TOOL_CALL_CONFLICT');
  const response = [{call_id: 'b', content: 'B'}, {call_id: 'a', content: 'A'}];
  finish(response);
  assert.equal((await early).content, 'B');
  assert.equal((await gate.execute('a', 'lookup', {query: 'x'})).content, 'A');
  assert.deepEqual(await batch, response);
  assert.throws(() => gate.announce('message', 'new-batch', [a]), error => error.code === 'TOOL_CALL_CONFLICT');
});

test('native gate cancels a call waiting for its batch', async () => {
  const controller = new AbortController(), gate = new ExternalToolGate({}, controller.signal);
  const result = gate.execute('unknown', 'lookup', {});
  controller.abort();
  await assert.rejects(result, error => error.code === 'CANCELLED');
});
