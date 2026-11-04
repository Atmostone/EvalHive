import { CheckCircle2, XCircle, Loader2, Circle, AlertTriangle, RefreshCw } from 'lucide-react'
import type { DecompositionResponse, DecompositionSubtask } from '@/types'

const STATUS_ICON: Record<string, JSX.Element> = {
  done: <CheckCircle2 className="h-4 w-4 text-green-600" />,
  failed: <XCircle className="h-4 w-4 text-red-600" />,
  in_progress: <Loader2 className="h-4 w-4 animate-spin text-blue-600" />,
  awaiting_approval: <Circle className="h-4 w-4 text-orange-500" />,
  ready: <Circle className="h-4 w-4 text-gray-400" />,
  decomposing: <Circle className="h-4 w-4 text-purple-400" />,
  backlog: <Circle className="h-4 w-4 text-gray-300" />,
  review: <Circle className="h-4 w-4 text-blue-400" />,
}

function statusIcon(status: string) {
  return STATUS_ICON[status] ?? <Circle className="h-4 w-4 text-gray-400" />
}

function durationStr(start: string | null, end: string | null): string {
  if (!start) return '—'
  const s = new Date(start).getTime()
  const e = end ? new Date(end).getTime() : Date.now()
  const sec = Math.max(0, Math.round((e - s) / 1000))
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
}

function depsHasWarning(s: DecompositionSubtask, siblingCount: number): boolean {
  return (
    s.depends_on.length === 0 &&
    siblingCount > 1 &&
    (s.status === 'failed' || s.retry_count > 0)
  )
}

interface Props {
  data: DecompositionResponse
}

export default function DecompositionTree({ data }: Props) {
  const { parent, subtasks } = data
  const titleById = new Map(subtasks.map((s) => [s.id, s.title]))

  const costs = [parent.cost_usd, ...subtasks.map((s) => s.cost_usd)]
  const totalCost = costs.includes(null) ? null : costs.reduce<number>((sum, cost) => sum + (cost ?? 0), 0)
  const totalRetries = subtasks.reduce((sum, s) => sum + (s.attempts.length - 1 < 0 ? 0 : s.attempts.length - 1), 0)
  const failedCount = subtasks.filter((s) => s.status === 'failed').length

  return (
    <div className="space-y-3">
      <div className="rounded border border-gray-200 bg-white p-4">
        <div className="flex items-start gap-2">
          {statusIcon(parent.status)}
          <div className="min-w-0 flex-1">
            <h3 className="truncate font-medium text-gray-900">{parent.title}</h3>
            <p className="mt-0.5 text-xs text-gray-500">
              {subtasks.length} subtasks · {durationStr(parent.started_at, parent.completed_at)} ·
              {' '}{totalCost == null ? 'Cost unavailable' : `$${totalCost.toFixed(4)} total`}
              {failedCount > 0 && <span className="ml-2 text-red-600">· {failedCount} failed</span>}
              {totalRetries > 0 && <span className="ml-2 text-amber-600">· {totalRetries} retries</span>}
            </p>
          </div>
        </div>
      </div>

      <div className="space-y-2">
        {subtasks.map((s) => {
          const warn = depsHasWarning(s, subtasks.length)
          const failedHard = s.status === 'failed' && s.retry_count >= s.max_retries
          return (
            <div
              key={s.id}
              className={
                'rounded border bg-white p-3 ' +
                (failedHard ? 'border-red-200 bg-red-50' : 'border-gray-200')
              }
            >
              <div className="flex items-start gap-2">
                {statusIcon(s.status)}
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <h4 className="truncate text-sm font-medium text-gray-900">{s.title}</h4>
                    <span className="text-xs text-gray-500">
                      {durationStr(s.started_at, s.completed_at)} · {s.cost_usd == null ? 'Cost unavailable' : `$${s.cost_usd.toFixed(4)}`}
                    </span>
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-600">
                    {s.template_name && (
                      <span className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-[10px] text-blue-700">
                        {s.template_name}
                      </span>
                    )}
                    <span>status: <strong className="font-mono">{s.status}</strong></span>
                    {s.attempts.length > 0 && (
                      <span>{s.attempts.length} attempt{s.attempts.length === 1 ? '' : 's'}</span>
                    )}
                    {s.retry_count > 0 && (
                      <span className="flex items-center gap-1 text-amber-700">
                        <RefreshCw className="h-3 w-3" />
                        {s.retry_count}/{s.max_retries} retry
                      </span>
                    )}
                    {s.result_files_count > 0 && (
                      <span>{s.result_files_count} file{s.result_files_count === 1 ? '' : 's'}</span>
                    )}
                  </div>

                  {s.depends_on.length > 0 && (
                    <p className="mt-1 text-xs text-gray-500">
                      ↳ depends on:{' '}
                      <span className="text-gray-700">
                        {s.depends_on.map((id) => titleById.get(id) ?? id.slice(0, 8)).join(', ')}
                      </span>
                    </p>
                  )}
                  {warn && (
                    <p className="mt-1 flex items-center gap-1 text-xs text-amber-700">
                      <AlertTriangle className="h-3.5 w-3.5" />
                      <span>
                        ⚠ no dependencies set — likely race condition with siblings.
                      </span>
                    </p>
                  )}

                  {s.attempts.some((a) => a.error) && (
                    <ul className="mt-2 space-y-0.5">
                      {s.attempts
                        .filter((a) => a.error)
                        .map((a) => (
                          <li key={a.agent_container_id} className="font-mono text-[11px] text-red-700">
                            ✗ {a.error}
                          </li>
                        ))}
                    </ul>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
