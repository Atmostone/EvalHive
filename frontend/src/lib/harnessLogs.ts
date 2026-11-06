import type { LogChunk } from '@/types'

// A tool row evolves while its position stays at the original call. REST and
// WebSocket delivery may race; a delayed snapshot cannot replace a newer result.
export function mergeLogChunks(current: LogChunk[], incoming: LogChunk[]): LogChunk[] {
  const rows = new Map<string, LogChunk>()
  for (const row of [...current, ...incoming]) {
    const key = row.id ?? `seq-${row.chunk_seq}`
    const previous = rows.get(key)
    if (!previous || (row.updated_seq ?? row.chunk_seq) >= (previous.updated_seq ?? previous.chunk_seq)) {
      rows.set(key, row)
    }
  }
  return [...rows.values()].sort((left, right) => left.chunk_seq - right.chunk_seq)
}
