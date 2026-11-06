import assert from 'node:assert/strict'
import test from 'node:test'
import { mergeLogChunks } from '../src/lib/harnessLogs.ts'

function row(id, start, update, content) {
  return { id, chunk_seq: start, updated_seq: update, content, created_at: null }
}

test('interleaved tool results update their own rows', () => {
  const initial = [row('root', 1, 1, ''), row('child', 2, 2, '')]
  const current = mergeLogChunks(initial, [row('child', 2, 3, 'Child'), row('root', 1, 4, 'Root')])
  assert.deepEqual(current.map((item) => item.content), ['Root', 'Child'])
})

test('a delayed REST snapshot cannot erase a WebSocket result', () => {
  const current = [row('call', 1, 8, 'Final result')]
  const snapshot = [row('call', 1, 3, 'Partial result')]
  assert.deepEqual(mergeLogChunks(current, snapshot), current)
})

test('duplicate delivery and legacy rows remain idempotent', () => {
  const rows = [row(null, 1, undefined, 'Legacy'), row('new', 2, 3, 'New')]
  assert.deepEqual(mergeLogChunks(rows, rows), rows)
})
