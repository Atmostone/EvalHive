import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, RefreshCw, AlertCircle, CheckCircle2 } from 'lucide-react'
import { qualityApi } from '@/api/client'
import type { FailureProfile, FailureClass } from '@/types'
import { cn } from '@/lib/utils'

/** Failure Mode Classifier (E-14): an LLM, on top of the trajectory judge (E-07),
 *  labels the trajectory with zero or more failure classes (multi-label), each
 *  with a confidence and a reason. A clean run yields no labels. */

interface Props {
  taskId: string
}

const CLASS_LABEL: Record<FailureClass, string> = {
  tool_confusion: 'Tool confusion',
  parameter_blind: 'Parameter-blind',
  loop: 'Loop',
  premature_stop: 'Premature stop',
  hallucinated_tool_result: 'Hallucinated result',
  ignored_error: 'Ignored error',
}

export default function FailureModePanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data, isFetching } = useQuery({
    queryKey: ['failure-profile', taskId],
    queryFn: () => qualityApi.getFailureModes(taskId),
    enabled: open,
    retry: false,
  })
  const profile = data?.failure_profile ?? null

  const evaluate = useMutation({
    mutationFn: () => qualityApi.evaluateFailureModes(taskId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['failure-profile', taskId] }),
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <AlertTriangle className="h-4 w-4" />
        Failure modes
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Failure modes (E-14)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {isFetching && <p className="text-xs text-gray-400">Loading…</p>}
      {!isFetching && profile && <ProfileView profile={profile} />}
      {!isFetching && !profile && <p className="text-xs text-gray-400">Not yet evaluated.</p>}

      {evaluate.isError && <p className="text-xs text-red-600">Evaluate request failed.</p>}
      {evaluate.data?.skipped && (
        <p className="text-xs text-amber-600">{evaluate.data.detail}</p>
      )}

      <button
        onClick={() => evaluate.mutate()}
        disabled={evaluate.isPending}
        className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-white disabled:opacity-50"
      >
        <RefreshCw className={cn('h-4 w-4', evaluate.isPending && 'animate-spin')} />
        {evaluate.isPending ? 'Classifying…' : profile ? 'Re-evaluate' : 'Classify failures'}
      </button>
    </div>
  )
}

function ProfileView({ profile }: { profile: FailureProfile }) {
  if (profile.status === 'error') {
    return (
      <div className="flex items-start gap-2 text-xs text-red-600">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span>Classifier error: {profile.errors[0]?.error ?? 'unknown'}</span>
      </div>
    )
  }

  return (
    <>
      {profile.failures.length === 0 ? (
        <div className="flex items-center gap-2 text-xs text-green-700">
          <CheckCircle2 className="h-4 w-4 shrink-0" />
          <span>No failure modes detected.</span>
        </div>
      ) : (
        <ul className="space-y-2">
          {profile.failures.map((f) => (
            <li key={f.class} className="text-xs">
              <div className="flex items-center gap-2">
                <span className="px-2 py-0.5 rounded-full font-medium bg-red-100 text-red-700">
                  {CLASS_LABEL[f.class] ?? f.class}
                </span>
                <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden max-w-[120px]">
                  <div
                    className="h-full bg-red-400"
                    style={{ width: `${Math.round(f.confidence * 100)}%` }}
                  />
                </div>
                <span className="text-gray-500 tabular-nums">
                  {Math.round(f.confidence * 100)}%
                </span>
              </div>
              {f.reason && <p className="text-gray-600 mt-0.5 ml-1">{f.reason}</p>}
            </li>
          ))}
        </ul>
      )}

      {profile.summary && <p className="text-xs text-gray-600 border-t pt-2">{profile.summary}</p>}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 border-t pt-2">
        <span className="text-gray-700">{profile.judge_model}</span>
        <span>${profile.judge_cost_usd.toFixed(4)}</span>
        <span>
          {profile.judge_input_tokens}/{profile.judge_output_tokens} tok
        </span>
        {profile.used_trajectory_profile && <span>+E-07</span>}
        {profile.used_outcome_profile && <span>+E-02</span>}
        {profile.input_capped && <span className="text-amber-600">input capped</span>}
      </div>
    </>
  )
}
