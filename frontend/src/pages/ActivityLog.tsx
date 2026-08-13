import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { eventsApi, buildWsUrl } from '@/api/client'
import { Clock, Download, Filter, Wifi, WifiOff } from 'lucide-react'
import { format } from 'date-fns'
import { SOURCE_COLORS } from '@/types'
import { cn } from '@/lib/utils'

const EVENT_TYPES = [
  'task_created', 'task_status_changed', 'orchestrator_decision',
  'agent_spawned', 'agent_completed', 'agent_failed', 'agent_killed', 'agent_message',
  'webhook_received', 'user_approval', 'user_rejection', 'task_retry', 'kill_all_agents',
  // SPA-84: every retry / add-config / retire-config on an experiment. Without it
  // in this list the mutation audit trail exists but is unreachable from the UI.
  'experiment_mutated', 'external_eval_verdict',
]

const SOURCES = ['orchestrator', 'agent', 'user', 'system']

export default function ActivityLog() {
  const queryClient = useQueryClient()
  const [filterSource, setFilterSource] = useState<string>('')
  const [filterType, setFilterType] = useState<string>('')
  const [filterTaskId, setFilterTaskId] = useState<string>('')
  const [wsConnected, setWsConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const filterRef = useRef({ source: '', type: '', taskId: '' })

  // Keep filter ref in sync
  useEffect(() => {
    filterRef.current = { source: filterSource, type: filterType, taskId: filterTaskId }
  }, [filterSource, filterType, filterTaskId])

  const { data: events = [] } = useQuery({
    queryKey: ['events', filterSource, filterType, filterTaskId],
    queryFn: () => eventsApi.list({
      source: filterSource || undefined,
      event_type: filterType || undefined,
      task_id: filterTaskId || undefined,
      limit: 200,
    }),
    refetchInterval: 30000, // fallback polling every 30s
  })

  // WebSocket for real-time event updates
  const connectWs = useCallback(() => {
    const ws = new WebSocket(buildWsUrl('/ws/events'))
    wsRef.current = ws

    ws.onopen = () => {
      setWsConnected(true)
      // Send current filters
      const f = filterRef.current
      ws.send(JSON.stringify({
        source: f.source || undefined,
        event_type: f.type || undefined,
        task_id: f.taskId || undefined,
      }))
    }

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'event') {
        const { type: _type, ...eventData } = data
        const f = filterRef.current
        queryClient.setQueryData(
          ['events', f.source, f.type, f.taskId],
          (old: unknown[] | undefined) => old ? [eventData, ...old].slice(0, 200) : [eventData]
        )
      }
    }

    ws.onclose = () => {
      setWsConnected(false)
      setTimeout(connectWs, 2000)
    }

    return ws
  }, [queryClient])

  useEffect(() => {
    const ws = connectWs()
    return () => { ws.close() }
  }, [connectWs])

  // Send filter updates over WebSocket when filters change
  useEffect(() => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        source: filterSource || undefined,
        event_type: filterType || undefined,
        task_id: filterTaskId || undefined,
      }))
    }
  }, [filterSource, filterType, filterTaskId])

  function getEventDescription(ev: typeof events[0]): string {
    const d = ev.data as Record<string, unknown>
    switch (ev.event_type) {
      case 'task_created': return `Task created: ${d.title || ''}`
      case 'task_status_changed': return `Status: ${d.old_status} → ${d.new_status}`
      case 'orchestrator_decision': return `${d.action}: ${d.reasoning || d.reason || d.template_name || ''}`
      case 'agent_spawned': return `Agent spawned: ${d.template_name} (${String(d.container_id || '').slice(0, 12)})`
      case 'agent_completed': return `Agent completed: ${String(d.result_summary || '').slice(0, 100)}`
      case 'agent_failed': return `Agent failed: ${d.error || ''}`
      case 'agent_killed': return `Agent killed: ${String(d.container_id || '').slice(0, 12)}`
      case 'webhook_received': return `Webhook: ${d.event || ''}`
      case 'user_approval': return 'User approved result'
      case 'user_rejection': return `User rejected: ${d.feedback || ''}`
      case 'task_retry': return `Retry #${d.retry}: ${d.error || d.reason || ''}`
      case 'kill_all_agents': return `Kill all: ${d.count} agents stopped`
      default: return ev.event_type
    }
  }

  return (
    <div className="p-6 h-full flex flex-col">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-bold text-gray-900">Activity Log</h1>
          {wsConnected ? (
            <span title="Real-time connected"><Wifi className="h-4 w-4 text-green-500" /></span>
          ) : (
            <span title="Reconnecting..."><WifiOff className="h-4 w-4 text-gray-400" /></span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {filterTaskId && (
            <a
              href={`/api/events/export/${filterTaskId}`}
              download
              className="flex items-center gap-1 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
            >
              <Download className="h-4 w-4" /> Export
            </a>
          )}
          <span className="text-sm text-gray-500">{events.length} events</span>
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-2 mb-4 flex-wrap items-center">
        <Filter className="h-4 w-4 text-gray-400" />
        <select value={filterSource} onChange={e => setFilterSource(e.target.value)}
          className="px-2 py-1.5 border rounded-lg text-sm bg-white">
          <option value="">All sources</option>
          {SOURCES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select value={filterType} onChange={e => setFilterType(e.target.value)}
          className="px-2 py-1.5 border rounded-lg text-sm bg-white">
          <option value="">All types</option>
          {EVENT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <input
          value={filterTaskId}
          onChange={e => setFilterTaskId(e.target.value)}
          placeholder="Filter by task ID..."
          className="px-2 py-1.5 border rounded-lg text-sm w-48"
        />
        {(filterSource || filterType || filterTaskId) && (
          <button onClick={() => { setFilterSource(''); setFilterType(''); setFilterTaskId('') }}
            className="text-xs text-blue-600 hover:underline">Clear</button>
        )}
      </div>

      {/* Event list */}
      <div className="flex-1 overflow-y-auto bg-white rounded-lg border">
        {events.length === 0 ? (
          <div className="p-8 text-center text-gray-500">No events found</div>
        ) : (
          <div className="divide-y">
            {events.map(ev => (
              <div key={ev.id} className="px-4 py-3 flex items-start gap-3 hover:bg-gray-50">
                <span className="text-xs text-gray-400 whitespace-nowrap pt-0.5 w-16 flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {format(new Date(ev.created_at), 'HH:mm:ss')}
                </span>
                <span className={cn('text-xs px-2 py-0.5 rounded whitespace-nowrap', SOURCE_COLORS[ev.source] || 'bg-gray-100')}>
                  {ev.source}
                </span>
                <span className="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-600 whitespace-nowrap">
                  {ev.event_type}
                </span>
                <span className="text-sm text-gray-700 flex-1 truncate">
                  {getEventDescription(ev)}
                </span>
                {ev.task_id && (
                  <span className="text-xs text-gray-400 whitespace-nowrap">
                    {ev.task_id.slice(0, 8)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
