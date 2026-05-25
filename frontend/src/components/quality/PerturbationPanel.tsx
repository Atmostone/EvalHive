import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldAlert, Play, Loader2, ShieldCheck } from 'lucide-react'
import { perturbationApi } from '@/api/client'
import type { PerturbationRun, PerturbationTransform, PerturbationTransformResult } from '@/types'
import { cn } from '@/lib/utils'

/** Adversarial / Perturbation Judge (E-12): replay this task under paraphrase /
 *  noise / reorder / injection transforms and compare each perturbed profile
 *  against a clean baseline → robustness score, plus a safety flag for whether
 *  the agent followed a prompt injected into a tool response. */

interface Props {
  taskId: string
}

const ACTIVE = new Set(['pending', 'running'])
const ALL_TRANSFORMS: PerturbationTransform[] = ['paraphrase', 'noise', 'reorder', 'inject']

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-600',
  running: 'bg-blue-100 text-blue-700',
  done: 'bg-green-100 text-green-700',
  capped: 'bg-amber-100 text-amber-700',
  failed: 'bg-red-100 text-red-700',
}

const TRANSFORM_LABEL: Record<PerturbationTransform, string> = {
  paraphrase: 'Paraphrase',
  noise: 'Noise',
  reorder: 'Reorder',
  inject: 'Injection',
}

export default function PerturbationPanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const [transforms, setTransforms] = useState<Set<PerturbationTransform>>(new Set(ALL_TRANSFORMS))
  const [variants, setVariants] = useState(1)
  const [baseN, setBaseN] = useState(2)
  const [costCap, setCostCap] = useState<string>('')
  const queryClient = useQueryClient()

  const { data: runs, isFetching } = useQuery({
    queryKey: ['perturbation-runs', taskId],
    queryFn: () => perturbationApi.listForTask(taskId),
    enabled: open,
    retry: false,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((r: PerturbationRun) => ACTIVE.has(r.status)) ? 5000 : false,
  })

  const mutation = useMutation({
    mutationFn: () =>
      perturbationApi.create({
        source_task_id: taskId,
        transforms: [...transforms],
        variants_per_transform: variants,
        base_n: baseN,
        cost_cap_usd: costCap ? Number(costCap) : undefined,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['perturbation-runs', taskId] }),
  })

  const toggle = (t: PerturbationTransform) =>
    setTransforms((prev) => {
      const next = new Set(prev)
      next.has(t) ? next.delete(t) : next.add(t)
      return next
    })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <ShieldAlert className="h-4 w-4" />
        Adversarial / perturbation
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Adversarial / perturbation (E-12)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {/* Launch form */}
      <div className="space-y-2 text-xs">
        <div className="flex flex-wrap gap-2">
          {ALL_TRANSFORMS.map((t) => (
            <label
              key={t}
              className={cn(
                'flex items-center gap-1.5 px-2 py-1 border rounded cursor-pointer',
                transforms.has(t) ? 'bg-white border-blue-300' : 'bg-gray-100 text-gray-400'
              )}
            >
              <input type="checkbox" checked={transforms.has(t)} onChange={() => toggle(t)} />
              {TRANSFORM_LABEL[t]}
            </label>
          ))}
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-gray-500">Variants / transform</span>
            <input
              type="number" min={1} max={5} value={variants}
              onChange={(e) => setVariants(Math.max(1, Math.min(5, Number(e.target.value) || 1)))}
              className="w-20 px-2 py-1 border rounded"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-gray-500">Baseline runs</span>
            <input
              type="number" min={1} max={10} value={baseN}
              onChange={(e) => setBaseN(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
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
          <button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || transforms.size === 0}
            className="flex items-center gap-2 px-3 py-1.5 border rounded-lg hover:bg-white disabled:opacity-50"
          >
            {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            {mutation.isPending ? 'Starting…' : 'Run perturbation'}
          </button>
        </div>
      </div>
      {mutation.isError && <p className="text-xs text-red-600">Failed to start perturbation run.</p>}

      {isFetching && !runs && <p className="text-xs text-gray-400">Loading…</p>}
      {runs && runs.length === 0 && <p className="text-xs text-gray-400">No perturbation runs yet.</p>}

      <div className="space-y-3">
        {(runs ?? []).map((run) => (
          <RunView key={run.id} run={run} />
        ))}
      </div>
    </div>
  )
}

function RunView({ run }: { run: PerturbationRun }) {
  const agg = run.aggregate
  const total = run.base_n + run.transforms.length * run.variants_per_transform
  const created =
    run.base_task_ids.length +
    Object.values(run.perturbed_task_ids ?? {}).reduce((s, ids) => s + ids.length, 0)

  return (
    <div className="border rounded-lg bg-white p-3 space-y-2">
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-500">
          {new Date(run.created_at ?? '').toLocaleString()} · {run.transforms.join(', ')}
        </span>
        <span className={cn('px-2 py-0.5 rounded-full', STATUS_STYLES[run.status] ?? 'bg-gray-100')}>
          {run.status}
        </span>
      </div>

      {ACTIVE.has(run.status) && (
        <p className="text-xs text-gray-500">
          {created}/{total} runs created · ${run.accumulated_cost_usd.toFixed(4)} spent…
        </p>
      )}

      {agg && (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-600">
            {agg.overall_robustness != null ? (
              <span>
                Overall robustness:{' '}
                <span className={cn('font-semibold', robustnessColor(agg.overall_robustness))}>
                  {Math.round(agg.overall_robustness * 100)}%
                </span>
              </span>
            ) : (
              <span className="text-gray-400">Robustness unavailable (no judge / baseline)</span>
            )}
            {agg.base.score != null && <span>Baseline score: {agg.base.score}/10</span>}
            <span>Cost: ${agg.accumulated_cost_usd.toFixed(4)}</span>
            {agg.capped && <span className="text-amber-600">cost-capped</span>}
          </div>

          {agg.safety && <SafetyBadge safety={agg.safety} />}

          <div className="border-t pt-2 space-y-1">
            {agg.transforms.map((t) => (
              <TransformRow key={t.key} t={t} />
            ))}
          </div>
        </>
      )}

      {run.status === 'failed' && agg?.error && <p className="text-xs text-red-600">{agg.error}</p>}
    </div>
  )
}

function TransformRow({ t }: { t: PerturbationTransformResult }) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-20 text-gray-600">{TRANSFORM_LABEL[t.key]}</span>
      <div className="flex-1 h-2 bg-gray-100 rounded overflow-hidden">
        {t.robustness != null && (
          <div
            className={cn('h-full', robustnessBg(t.robustness))}
            style={{ width: `${Math.round(t.robustness * 100)}%` }}
          />
        )}
      </div>
      <span className="w-24 text-right text-gray-600">
        {t.robustness != null ? `${Math.round(t.robustness * 100)}%` : 'n/a'}
        {t.score_delta != null && (
          <span className={t.score_delta < 0 ? 'text-red-500' : 'text-green-600'}>
            {' '}
            ({t.score_delta > 0 ? '+' : ''}
            {t.score_delta})
          </span>
        )}
      </span>
      <span className="w-12 text-right text-gray-400">
        {t.n_success}/{t.n_total}
      </span>
    </div>
  )
}

function SafetyBadge({ safety }: { safety: NonNullable<PerturbationRun['aggregate']>['safety'] }) {
  if (!safety) return null
  const followed = safety.injection_followed
  return (
    <div
      className={cn(
        'flex items-center gap-2 px-2 py-1 rounded text-xs',
        followed ? 'bg-red-50 text-red-700' : 'bg-green-50 text-green-700'
      )}
    >
      {followed ? <ShieldAlert className="h-4 w-4" /> : <ShieldCheck className="h-4 w-4" />}
      {followed
        ? `Followed prompt injection in ${safety.followed_count}/${safety.n} run(s)`
        : `Resisted prompt injection (0/${safety.n})`}
    </div>
  )
}

function robustnessColor(r: number): string {
  if (r >= 0.85) return 'text-green-600'
  if (r >= 0.6) return 'text-amber-600'
  return 'text-red-600'
}

function robustnessBg(r: number): string {
  if (r >= 0.85) return 'bg-green-500'
  if (r >= 0.6) return 'bg-amber-500'
  return 'bg-red-500'
}
