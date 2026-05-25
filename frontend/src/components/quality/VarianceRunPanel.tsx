import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Dices, Play, Loader2 } from 'lucide-react'
import { varianceApi } from '@/api/client'
import type { VarianceRun, VarianceDimension } from '@/types'
import { cn } from '@/lib/utils'
import BoxPlot, { type BoxPlotDatum } from './BoxPlot'

/** Variance / Robustness Harness (E-11): replay this task N times and show the
 *  dispersion of outcome score, trajectory length, success rate and tool
 *  selection — an agent that is sometimes brilliant and sometimes fails is
 *  worse than a stably-mediocre one. */

interface Props {
  taskId: string
}

const ACTIVE = new Set(['pending', 'running'])

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-600',
  running: 'bg-blue-100 text-blue-700',
  done: 'bg-green-100 text-green-700',
  capped: 'bg-amber-100 text-amber-700',
  failed: 'bg-red-100 text-red-700',
}

export default function VarianceRunPanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const [n, setN] = useState(5)
  const [parallel, setParallel] = useState(true)
  const [costCap, setCostCap] = useState<string>('')
  const queryClient = useQueryClient()

  const { data: runs, isFetching } = useQuery({
    queryKey: ['variance-runs', taskId],
    queryFn: () => varianceApi.listForTask(taskId),
    enabled: open,
    retry: false,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((r: VarianceRun) => ACTIVE.has(r.status)) ? 5000 : false,
  })

  const mutation = useMutation({
    mutationFn: () =>
      varianceApi.create({
        source_task_id: taskId,
        n,
        parallel,
        cost_cap_usd: costCap ? Number(costCap) : undefined,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['variance-runs', taskId] }),
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <Dices className="h-4 w-4" />
        Variance / robustness
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Variance / robustness (E-11)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {/* Launch form */}
      <div className="flex flex-wrap items-end gap-3 text-xs">
        <label className="flex flex-col gap-1">
          <span className="text-gray-500">Runs (N)</span>
          <input
            type="number" min={2} max={50} value={n}
            onChange={(e) => setN(Math.max(2, Math.min(50, Number(e.target.value) || 2)))}
            className="w-20 px-2 py-1 border rounded"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-gray-500">Cost cap ($)</span>
          <input
            type="number" min={0} step="0.01" value={costCap} placeholder="none"
            onChange={(e) => setCostCap(e.target.value)}
            className="w-24 px-2 py-1 border rounded"
          />
        </label>
        <label className="flex items-center gap-1.5 pb-1.5">
          <input type="checkbox" checked={parallel} onChange={(e) => setParallel(e.target.checked)} />
          <span className="text-gray-600">parallel</span>
        </label>
        <button
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="flex items-center gap-2 px-3 py-1.5 border rounded-lg hover:bg-white disabled:opacity-50"
        >
          {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
          {mutation.isPending ? 'Starting…' : 'Run variance'}
        </button>
      </div>
      {mutation.isError && <p className="text-xs text-red-600">Failed to start variance run.</p>}

      {isFetching && !runs && <p className="text-xs text-gray-400">Loading…</p>}
      {runs && runs.length === 0 && <p className="text-xs text-gray-400">No variance runs yet.</p>}

      <div className="space-y-3">
        {(runs ?? []).map((run) => (
          <RunView key={run.id} run={run} />
        ))}
      </div>
    </div>
  )
}

function RunView({ run }: { run: VarianceRun }) {
  const agg = run.aggregate
  return (
    <div className="border rounded-lg bg-white p-3 space-y-2">
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-500">
          {new Date(run.created_at ?? '').toLocaleString()} · n={run.n}
          {run.parallel ? '' : ' · sequential'}
        </span>
        <span className={cn('px-2 py-0.5 rounded-full', STATUS_STYLES[run.status] ?? 'bg-gray-100')}>
          {run.status}
        </span>
      </div>

      {ACTIVE.has(run.status) && (
        <p className="text-xs text-gray-500">
          {run.child_task_ids.length}/{run.n} runs created · ${run.accumulated_cost_usd.toFixed(4)} spent…
        </p>
      )}

      {agg && (
        <>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600">
            <span>
              Success: <span className="font-medium">{Math.round(agg.success_rate * 100)}%</span>{' '}
              ({agg.n_success}/{agg.n_executed})
            </span>
            <span>Cost: ${agg.accumulated_cost_usd.toFixed(4)}</span>
            {agg.capped && <span className="text-amber-600">cost-capped</span>}
          </div>

          <BoxPlot data={dimToBoxData(agg.dimensions)} />

          {agg.tool_stability.runs > 0 && (
            <div className="text-xs text-gray-600 border-t pt-2">
              <span className="text-gray-500">Tool stability: </span>
              {agg.tool_stability.distinct_signatures} distinct path
              {agg.tool_stability.distinct_signatures === 1 ? '' : 's'}
              {agg.tool_stability.modal_share != null && (
                <span> · modal path in {Math.round(agg.tool_stability.modal_share * 100)}% of runs</span>
              )}
              {agg.tool_stability.per_tool.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-gray-500">
                  {agg.tool_stability.per_tool.map((t) => (
                    <span key={t.tool}>
                      {t.tool}: {t.mean}±{t.std}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}

      {run.status === 'failed' && !agg?.dimensions && (
        <p className="text-xs text-red-600">{agg?.error ?? 'run failed'}</p>
      )}
    </div>
  )
}

function dimToBoxData(dimensions: VarianceDimension[]): BoxPlotDatum[] {
  return dimensions
    .filter((d) => d.available && d.dist.n > 0)
    .map((d) => ({
      label: d.name,
      unit: d.unit,
      dist: d.dist,
      domain: d.unit === '0-10' ? ([0, 10] as [number, number]) : undefined,
    }))
}
