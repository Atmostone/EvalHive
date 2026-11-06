import type { AttemptEvent } from '../types/index.ts'

export function mergeInteractions(previous: Record<string, AttemptEvent>, events: AttemptEvent[]) {
  const next = { ...previous }
  for (const event of events) {
    const id = event.data.request_id
    if (!id) continue
    if (event.event === 'interaction.resolved') delete next[id]
    else if (event.event === 'interaction.request') next[id] = event
  }
  return next
}
