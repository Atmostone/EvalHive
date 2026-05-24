import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { qualityApi } from '@/api/client'
import { Scissors } from 'lucide-react'
import type { CleanedTraceStepKind } from '@/types'
import { cn } from '@/lib/utils'

/** Trace Cleaner (E-06): preview the compact, judge-ready trajectory that the
 *  trajectory judge (E-07) will consume. Read-only; computed on demand. */

const KIND_STYLE: Record<CleanedTraceStepKind, string> = {
  reasoning: 'bg-purple-100 text-purple-700',
  tool: 'bg-blue-100 text-blue-700',
  agent: 'bg-gray-100 text-gray-700',
}

interface Props {
  taskId: string
}

export default function CleanedTracePanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const [keepTail, setKeepTail] = useState(false)

  const { data, isFetching, isError } = useQuery({
    queryKey: ['cleaned-trace', taskId, keepTail],
    queryFn: () => qualityApi.getCleanedTrace(taskId, { keep_tail_on_error: keepTail }),
    enabled: open,
    retry: false,
  })
  const trace = data?.cleaned_trace ?? null

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <Scissors className="h-4 w-4" />
        View cleaned trace
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Cleaned trace (judge input)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {isFetching && <p className="text-xs text-gray-400">Cleaning…</p>}
      {isError && <p className="text-xs text-red-600">Failed to build cleaned trace.</p>}

      {trace && (
        <>
          {trace.error && <p className="text-xs text-red-600">Cleaner error: {trace.error}</p>}

          {/* Token savings */}
          <div className="flex items-center gap-2 text-xs">
            <span className="text-gray-500">
              {trace.stats.original_tokens.toLocaleString()} → {trace.stats.cleaned_tokens.toLocaleString()} tokens
            </span>
            <span
              className={cn(
                'px-1.5 py-0.5 rounded font-medium',
                trace.stats.savings_pct > 0 ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600',
              )}
            >
              −{trace.stats.savings_pct}%
            </span>
            <span className="text-gray-400">
              {trace.stats.steps_total} steps · {trace.stats.steps_truncated} truncated · {trace.stats.events_dropped} dropped
            </span>
          </div>

          <label className="flex items-center gap-2 text-xs text-gray-600">
            <input type="checkbox" checked={keepTail} onChange={(e) => setKeepTail(e.target.checked)} />
            keep tail on error (don't truncate failed steps)
          </label>

          {/* Steps */}
          {trace.steps.length === 0 ? (
            <p className="text-xs text-gray-400">No trajectory steps recorded.</p>
          ) : (
            <div className="space-y-2 max-h-96 overflow-y-auto">
              {trace.steps.map((s) => (
                <div key={s.seq} className="text-xs">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-gray-400">#{s.seq}</span>
                    <span className={cn('px-1.5 py-0.5 rounded', KIND_STYLE[s.kind])}>{s.kind}</span>
                    {s.tool_name && <span className="text-gray-600 font-mono">{s.tool_name}</span>}
                    {s.truncated && (
                      <span className="text-amber-600">
                        {s.kept_tokens}/{s.original_tokens} tok
                      </span>
                    )}
                  </div>
                  <pre className="whitespace-pre-wrap break-words bg-white border rounded p-2 text-gray-700 font-mono">
                    {s.content || '∅'}
                  </pre>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
