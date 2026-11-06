import assert from 'node:assert/strict'
import test from 'node:test'
import { mergeInteractions } from '../src/lib/harnessInteractions.ts'

test('interleaved interactions resolve by request identity and replay stays stable', () => {
  const ask = (id, seq) => ({ event: 'interaction.request', seq, data: { request_id: id, kind: 'approval' } })
  const pending = mergeInteractions({}, [ask('first', 1), ask('second', 2)])
  assert.deepEqual(Object.keys(pending), ['first', 'second'])
  const events = [{ event: 'interaction.resolved', seq: 3, data: { request_id: 'second' } }]
  const resolved = mergeInteractions(pending, events)
  assert.deepEqual(Object.keys(resolved), ['first'])
  assert.deepEqual(mergeInteractions(resolved, events), resolved)
  assert.deepEqual(mergeInteractions({}, [ask('first', 1), ...events, ask('third', 4)]), {
    first: ask('first', 1), third: ask('third', 4),
  })
})
