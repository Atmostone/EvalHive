import { useCallback, useEffect, useRef, useState } from 'react'
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso'
import { Archive, Terminal } from 'lucide-react'
import { buildWsUrl, logsApi } from '@/api/client'
import type { LogChunk } from '@/types'

interface AgentLogViewerProps {
  taskId: string
  archived: boolean
}

const PAGE_SIZE = 200

export default function AgentLogViewer({ taskId, archived }: AgentLogViewerProps) {
  const [chunks, setChunks] = useState<LogChunk[]>([])
  const [loading, setLoading] = useState(true)
  const [hasEarlier, setHasEarlier] = useState(false)
  const [follow, setFollow] = useState(true)
  const virtuosoRef = useRef<VirtuosoHandle | null>(null)
  const seenIds = useRef<Set<string>>(new Set())
  const seenSeq = useRef<Set<number>>(new Set())
  const wsRef = useRef<WebSocket | null>(null)

  const remember = useCallback((c: LogChunk) => {
    if (c.id) seenIds.current.add(c.id)
    seenSeq.current.add(c.chunk_seq)
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    logsApi
      .list(taskId, { limit: PAGE_SIZE })
      .then((resp) => {
        if (cancelled) return
        resp.chunks.forEach(remember)
        setChunks(resp.chunks)
        setHasEarlier(resp.chunks.length === PAGE_SIZE)
        setLoading(false)
      })
      .catch(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [taskId, remember])

  useEffect(() => {
    if (archived) return
    const ws = new WebSocket(buildWsUrl(`/ws/tasks/${taskId}/log`))
    wsRef.current = ws

    ws.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data)
        if (payload.type !== 'log_chunk') return
        const chunk: LogChunk = {
          id: payload.id ?? null,
          chunk_seq: payload.chunk_seq,
          content: payload.content,
          tool_name: payload.tool_name ?? null,
          reasoning: payload.reasoning ?? null,
          created_at: payload.created_at ?? null,
        }
        const dupKey = chunk.id ?? `seq-${chunk.chunk_seq}`
        if (chunk.id && seenIds.current.has(chunk.id)) return
        if (!chunk.id && seenSeq.current.has(chunk.chunk_seq)) return
        seenIds.current.add(dupKey)
        seenSeq.current.add(chunk.chunk_seq)
        setChunks((prev) => [...prev, chunk])
      } catch {
        /* ignore */
      }
    }

    return () => {
      ws.close()
      wsRef.current = null
    }
  }, [taskId, archived])

  const loadEarlier = useCallback(async () => {
    if (chunks.length === 0) return
    const minSeq = chunks[0].chunk_seq
    const fromSeq = Math.max(0, minSeq - PAGE_SIZE)
    if (fromSeq === minSeq) return
    const resp = await logsApi.list(taskId, { from_seq: fromSeq, limit: minSeq - fromSeq })
    const fresh = resp.chunks.filter((c) => !seenSeq.current.has(c.chunk_seq))
    fresh.forEach(remember)
    setChunks((prev) => [...fresh, ...prev])
    setHasEarlier(fromSeq > 0)
  }, [chunks, taskId, remember])

  if (loading) {
    return (
      <div className="text-sm text-gray-500 flex items-center gap-2">
        <Terminal className="h-4 w-4" /> Loading logs…
      </div>
    )
  }

  if (chunks.length === 0) {
    return null
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-gray-500 flex items-center gap-1.5">
          <Terminal className="h-4 w-4" />
          Logs ({chunks.length})
        </h3>
        <div className="flex items-center gap-2 text-xs">
          {archived && (
            <span className="flex items-center gap-1 text-gray-500">
              <Archive className="h-3 w-3" />
              archived from MinIO
            </span>
          )}
          {hasEarlier && (
            <button
              onClick={loadEarlier}
              className="text-blue-600 hover:underline"
              type="button"
            >
              Load earlier
            </button>
          )}
          <label className="flex items-center gap-1 text-gray-500 cursor-pointer">
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
              className="h-3 w-3"
            />
            follow
          </label>
        </div>
      </div>
      <div className="rounded-md border border-gray-800 bg-gray-900 text-gray-100 font-mono text-xs">
        <Virtuoso
          ref={virtuosoRef}
          style={{ height: 360 }}
          data={chunks}
          followOutput={follow ? 'auto' : false}
          itemContent={(_index, c) => (
            <div className="px-3 py-1 border-b border-gray-800/60">
              {c.tool_name && (
                <div className="text-emerald-300/70 text-[10px] uppercase tracking-wide">
                  #{c.chunk_seq} · {c.tool_name}
                </div>
              )}
              {c.reasoning && (
                <div
                  className="text-violet-300/60 italic whitespace-pre-wrap break-words leading-tight mb-1"
                  title="the model's own deliberation before this step — stored apart from what it said"
                >
                  {c.reasoning}
                </div>
              )}
              {c.content && (
                <pre className="whitespace-pre-wrap break-words leading-tight m-0">
                  {c.content}
                </pre>
              )}
            </div>
          )}
        />
      </div>
    </div>
  )
}
