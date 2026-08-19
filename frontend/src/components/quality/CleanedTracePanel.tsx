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
  attempt: 'bg-amber-100 text-amber-800',
}

interface Props {
  taskId: string
}

export default function CleanedTracePanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const [keepTail, setKeepTail] = useState(false)
  // 0 is the «off» sentinel the backend understands (SPA-86): with a 1M-context
  // judge, reading the whole trajectory is a legitimate request, and the only way
  // to see what the caps are actually costing is to be able to turn them off.
  const [noTrim, setNoTrim] = useState(false)

  const { data, isFetching, isError } = useQuery({
    queryKey: ['cleaned-trace', taskId, keepTail, noTrim],
    queryFn: () =>
      qualityApi.getCleanedTrace(taskId, {
        keep_tail_on_error: keepTail,
        ...(noTrim ? { tool_output_token_cap: 0, tool_args_token_cap: 0 } : {}),
      }),
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
              {trace.stats.steps_total} steps · {trace.stats.steps_truncated} truncated
              {trace.stats.steps_args_truncated ? ` · ${trace.stats.steps_args_truncated} args truncated` : ''} ·{' '}
              {trace.stats.events_dropped} dropped
            </span>
          </div>

          <div className="space-y-1">
            <label className="flex items-center gap-2 text-xs text-gray-600">
              <input type="checkbox" checked={keepTail} onChange={(e) => setKeepTail(e.target.checked)} />
              keep tail on error (don't truncate failed steps)
            </label>
            <label className="flex items-center gap-2 text-xs text-gray-600">
              <input type="checkbox" checked={noTrim} onChange={(e) => setNoTrim(e.target.checked)} />
              no truncation — show the trace in full (outputs and arguments uncapped)
            </label>
          </div>

          {/* Steps */}
          {trace.steps.length === 0 ? (
            <p className="text-xs text-gray-400">No trajectory steps recorded.</p>
          ) : (
            <div className="space-y-2 max-h-96 overflow-y-auto">
              {trace.steps.map((s) =>
                /* SPA-113: a boundary is not a step the agent took, so it reads as a
                   rule across the trace rather than as another numbered action. */
                s.kind === 'attempt' ? (
                  <div key={s.seq} className="flex items-center gap-2 py-1">
                    <div className="h-px flex-1 bg-amber-300" />
                    <span className="text-[10px] uppercase tracking-wide text-amber-700 font-medium whitespace-nowrap">
                      {s.content.replace(/^──\s*|\s*──$/g, '')}
                    </span>
                    <div className="h-px flex-1 bg-amber-300" />
                  </div>
                ) : (
                <div key={s.seq} className="text-xs">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-gray-400">#{s.seq}</span>
                    <span className={cn('px-1.5 py-0.5 rounded', KIND_STYLE[s.kind])}>{s.kind}</span>
                    {s.tool_name && <span className="text-gray-600 font-mono">{s.tool_name}</span>}
                    {s.arguments_truncated && (
                      <span
                        className="text-amber-600"
                        title="A long argument value was shortened. Every parameter the agent passed is still listed — only values shrink, keys are never dropped."
                      >
                        args truncated
                      </span>
                    )}
                    {s.result_missing && (
                      <span
                        className="text-red-600"
                        title="The call was recorded before the tool ran, but no result ever arrived — the tool hung, crashed, or raised. The call itself is still here, which is the point of recording it first."
                      >
                        no result
                      </span>
                    )}
                    {!!s.parts_missing && (
                      <span
                        className="text-red-600"
                        title="Part of this tool's output never reached the backend. The gap is marked in the text rather than spliced over, which would fabricate a contiguous output that never existed."
                      >
                        {s.parts_missing} part(s) missing
                      </span>
                    )}
                    {s.truncated && (
                      <span className="text-amber-600">
                        {s.kept_tokens}/{s.original_tokens} tok
                      </span>
                    )}
                  </div>
                  {/* The CALL, above its result — `parameter_quality` and
                      `tool_selection` are questions about this half of the step,
                      and until SPA-86 it was not recorded at all. */}
                  {s.tool_name && (
                    <pre className="whitespace-pre-wrap break-words bg-blue-50/60 border border-blue-100 rounded p-2 mb-1 text-blue-900 font-mono">
                      {s.arguments
                        ? JSON.stringify(s.arguments, null, 2)
                        : '(no arguments recorded for this call)'}
                    </pre>
                  )}
                  <pre className="whitespace-pre-wrap break-words bg-white border rounded p-2 text-gray-700 font-mono">
                    {s.content || '∅'}
                  </pre>
                </div>
                ),
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
