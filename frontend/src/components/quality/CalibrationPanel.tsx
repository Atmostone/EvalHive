import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Gauge, RefreshCw, AlertCircle, CheckCircle2, XCircle } from 'lucide-react'
import { qualityApi } from '@/api/client'
import type { CalibrationProfile, CalibrationMetrics, ReliabilityBucket } from '@/types'
import { cn } from '@/lib/utils'

/** Confidence Calibration (E-16): a post-hoc self-probe asks the doer model to
 *  estimate P(its own answer is correct) WITHOUT seeing the grader's verdict.
 *  Paired with E-02 correctness, this yields a per-task (confidence, correct)
 *  point and a Brier term; the workspace aggregate turns many points into
 *  ECE / Brier / a reliability diagram with a per-model recommendation. */

interface Props {
  taskId: string
}

export default function CalibrationPanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data, isFetching } = useQuery({
    queryKey: ['calibration-profile', taskId],
    queryFn: () => qualityApi.getCalibration(taskId),
    enabled: open,
    retry: false,
  })
  const profile = data?.calibration_profile ?? null

  const aggregate = useQuery({
    queryKey: ['calibration-aggregate'],
    queryFn: () => qualityApi.getCalibrationAggregate(),
    enabled: open,
    retry: false,
  })

  const evaluate = useMutation({
    mutationFn: () => qualityApi.evaluateCalibration(taskId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['calibration-profile', taskId] })
      queryClient.invalidateQueries({ queryKey: ['calibration-aggregate'] })
    },
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <Gauge className="h-4 w-4" />
        Calibration
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Calibration (E-16)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {isFetching && <p className="text-xs text-gray-400">Loading…</p>}
      {!isFetching && profile && <ProfileView profile={profile} />}
      {!isFetching && !profile && <p className="text-xs text-gray-400">Not yet evaluated.</p>}

      {evaluate.isError && <p className="text-xs text-red-600">Evaluate request failed.</p>}
      {evaluate.data?.skipped && <p className="text-xs text-amber-600">{evaluate.data.detail}</p>}

      <button
        onClick={() => evaluate.mutate()}
        disabled={evaluate.isPending}
        className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-white disabled:opacity-50"
      >
        <RefreshCw className={cn('h-4 w-4', evaluate.isPending && 'animate-spin')} />
        {evaluate.isPending ? 'Probing…' : profile ? 'Re-evaluate' : 'Evaluate'}
      </button>

      <div className="border-t pt-2 space-y-2">
        <h5 className="text-xs font-medium text-gray-600">Workspace calibration</h5>
        {aggregate.isFetching && <p className="text-xs text-gray-400">Loading…</p>}
        {!aggregate.isFetching && aggregate.data && aggregate.data.overall.count > 0 && (
          <AggregateView
            overall={aggregate.data.overall}
            byModel={aggregate.data.by_model}
            recommendations={aggregate.data.recommendations}
          />
        )}
        {!aggregate.isFetching && aggregate.data && aggregate.data.overall.count === 0 && (
          <p className="text-xs text-gray-400">No scored calibration records yet.</p>
        )}
      </div>
    </div>
  )
}

function ProfileView({ profile }: { profile: CalibrationProfile }) {
  if (profile.status === 'error') {
    return (
      <div className="flex items-start gap-2 text-xs text-red-600">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span>Probe error: {profile.errors[0]?.error ?? 'unknown'}</span>
      </div>
    )
  }

  const conf = profile.predicted_confidence ?? 0
  return (
    <>
      <div className="space-y-1">
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-600">Predicted confidence</span>
          <span className="tabular-nums font-medium text-gray-800">
            {Math.round(conf * 100)}%
          </span>
        </div>
        <div className="h-2 rounded-full bg-gray-200 overflow-hidden">
          <div
            className="h-full bg-indigo-500"
            style={{ width: `${Math.round(conf * 100)}%` }}
          />
        </div>
      </div>

      <div className="flex items-center gap-3 text-xs">
        {profile.actual_correct ? (
          <span className="flex items-center gap-1 text-green-700">
            <CheckCircle2 className="h-4 w-4 shrink-0" /> Actually correct
          </span>
        ) : (
          <span className="flex items-center gap-1 text-red-700">
            <XCircle className="h-4 w-4 shrink-0" /> Actually incorrect
          </span>
        )}
        {profile.brier_term != null && (
          <span className="ml-auto px-2 py-0.5 rounded-full font-medium bg-gray-200 text-gray-700 tabular-nums">
            Brier {profile.brier_term.toFixed(3)}
          </span>
        )}
      </div>

      {profile.reasoning && (
        <p className="text-xs text-gray-600 border-t pt-2">{profile.reasoning}</p>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 border-t pt-2">
        <span className="text-gray-700">{profile.probe_model}</span>
        <span>via {profile.outcome_signal}</span>
        {profile.outcome_score != null && (
          <span className="tabular-nums">
            score {profile.outcome_score.toFixed(1)}/{profile.outcome_threshold.toFixed(0)}
          </span>
        )}
        <span>${profile.judge_cost_usd.toFixed(4)}</span>
        <span>
          {profile.judge_input_tokens}/{profile.judge_output_tokens} tok
        </span>
        {profile.used_outcome_profile && <span>+E-02</span>}
        {profile.input_capped && <span className="text-amber-600">input capped</span>}
      </div>
    </>
  )
}

function AggregateView({
  overall,
  byModel,
  recommendations,
}: {
  overall: CalibrationMetrics
  byModel: Record<string, CalibrationMetrics>
  recommendations: string[]
}) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <Metric label="ECE" value={overall.ece} />
        <Metric label="Brier" value={overall.brier} />
        <Metric label="Accuracy" value={overall.accuracy} percent />
        <Metric label="Overconf." value={overall.overconfidence} percent signed />
      </div>

      <ReliabilityDiagram buckets={overall.reliability} />

      {recommendations.length > 0 && (
        <ul className="space-y-1 border-t pt-2">
          {recommendations.map((r, i) => (
            <li key={i} className="text-xs text-gray-600">
              {r}
            </li>
          ))}
        </ul>
      )}

      {Object.keys(byModel).length > 1 && (
        <div className="space-y-1 border-t pt-2">
          {Object.entries(byModel).map(([model, m]) => (
            <div key={model} className="flex items-center justify-between text-xs">
              <span className="text-gray-700 truncate">{model}</span>
              <span className="text-gray-500 tabular-nums">
                n={m.count} · ECE {m.ece != null ? m.ece.toFixed(3) : '—'} · acc{' '}
                {m.accuracy != null ? `${Math.round(m.accuracy * 100)}%` : '—'}
              </span>
            </div>
          ))}
        </div>
      )}

      <p className="text-[10px] text-gray-400">n={overall.count} scored tasks</p>
    </div>
  )
}

function Metric({
  label,
  value,
  percent,
  signed,
}: {
  label: string
  value: number | null
  percent?: boolean
  signed?: boolean
}) {
  let text = '—'
  if (value != null) {
    if (percent) {
      const p = Math.round(value * 100)
      text = `${signed && p > 0 ? '+' : ''}${p}%`
    } else {
      text = value.toFixed(3)
    }
  }
  return (
    <div className="rounded-lg bg-white border px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-gray-400">{label}</div>
      <div className="tabular-nums font-medium text-gray-800">{text}</div>
    </div>
  )
}

/** Reliability diagram: per-bucket bars of mean confidence (light) vs accuracy
 *  (dark). A perfectly calibrated model has the two equal in every bucket. */
function ReliabilityDiagram({ buckets }: { buckets: ReliabilityBucket[] }) {
  const filled = buckets.filter((b) => b.count > 0)
  if (filled.length === 0) {
    return <p className="text-xs text-gray-400 italic">No reliability data.</p>
  }
  const H = 90
  return (
    <div className="space-y-1">
      <div className="flex items-end gap-1" style={{ height: H }}>
        {buckets.map((b, i) => {
          const empty = b.count === 0
          const conf = b.avg_confidence ?? 0
          const acc = b.accuracy ?? 0
          return (
            <div key={i} className="flex-1 flex flex-col justify-end items-center h-full">
              <div className="relative w-full flex items-end justify-center h-full">
                {!empty && (
                  <>
                    <div
                      className="w-full rounded-t bg-indigo-200"
                      style={{ height: `${conf * 100}%` }}
                      title={`avg confidence ${Math.round(conf * 100)}%`}
                    />
                    <div
                      className="absolute bottom-0 left-1/4 w-1/2 rounded-t bg-indigo-600"
                      style={{ height: `${acc * 100}%` }}
                      title={`accuracy ${Math.round(acc * 100)}% (n=${b.count})`}
                    />
                  </>
                )}
              </div>
            </div>
          )
        })}
      </div>
      <div className="flex gap-1 text-[9px] text-gray-400 tabular-nums">
        {buckets.map((b, i) => (
          <span key={i} className="flex-1 text-center">
            {Math.round(b.lo * 100)}
          </span>
        ))}
      </div>
      <div className="flex items-center gap-3 text-[10px] text-gray-500">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm bg-indigo-200" /> confidence
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm bg-indigo-600" /> accuracy
        </span>
        <span className="ml-auto">confidence bin (%)</span>
      </div>
    </div>
  )
}
