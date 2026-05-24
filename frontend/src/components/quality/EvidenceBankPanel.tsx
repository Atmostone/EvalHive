import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  ResponsiveContainer,
  Tooltip,
} from 'recharts'
import { Database, RefreshCw, AlertCircle } from 'lucide-react'
import { qualityApi } from '@/api/client'
import type { TrajectoryEvidenceProfile } from '@/types'
import { cn } from '@/lib/utils'

/** TRACE Evidence Bank Judge (E-08): walks the cleaned trace (E-06) step by step,
 *  accumulating an evidence bank that is fed into each step's judgement, then
 *  produces an evidence-aware 6-axis profile (comparable to E-07) plus a
 *  groundedness signal — how much the outcome rests on gathered evidence vs luck. */

interface Props {
  taskId: string
}

export default function EvidenceBankPanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data, isFetching } = useQuery({
    queryKey: ['trace-evidence-profile', taskId],
    queryFn: () => qualityApi.getTraceEvidenceProfile(taskId),
    enabled: open,
    retry: false,
  })
  const profile = data?.trajectory_evidence_profile ?? null

  const mutation = useMutation({
    mutationFn: () => qualityApi.evaluateTraceEvidence(taskId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['trace-evidence-profile', taskId] }),
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <Database className="h-4 w-4" />
        Evidence bank score
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Evidence bank score (E-08)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {isFetching && <p className="text-xs text-gray-400">Loading…</p>}

      {!isFetching && profile && <ProfileView profile={profile} />}

      {!isFetching && !profile && <p className="text-xs text-gray-400">Not yet judged.</p>}

      {mutation.isError && <p className="text-xs text-red-600">Evaluation request failed.</p>}
      {mutation.data?.skipped && (
        <p className="text-xs text-amber-600">{mutation.data.detail}</p>
      )}

      <button
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-white disabled:opacity-50"
      >
        <RefreshCw className={cn('h-4 w-4', mutation.isPending && 'animate-spin')} />
        {mutation.isPending ? 'Judging…' : profile ? 'Re-evaluate' : 'Evaluate evidence bank'}
      </button>
    </div>
  )
}

function ProfileView({ profile }: { profile: TrajectoryEvidenceProfile }) {
  if (profile.status === 'error') {
    return (
      <div className="flex items-start gap-2 text-xs text-red-600">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span>Judge error: {profile.errors[0]?.error ?? 'unknown'}</span>
      </div>
    )
  }

  const data = profile.axes.map((a) => ({ axis: a.name, score: a.score }))

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm">
          Overall:{' '}
          <span className="font-medium text-gray-700">
            {profile.overall_score != null ? `${profile.overall_score}/10` : '—'}
          </span>
        </span>
        {profile.groundedness != null && (
          <span
            className={cn(
              'text-xs px-2 py-0.5 rounded-full',
              profile.groundedness >= 0.7
                ? 'bg-green-100 text-green-700'
                : profile.groundedness >= 0.4
                  ? 'bg-amber-100 text-amber-700'
                  : 'bg-red-100 text-red-700',
            )}
            title="share of steps grounded in the accumulated evidence (low = lucky/guessed)"
          >
            Grounded {Math.round(profile.groundedness * 100)}%
          </span>
        )}
        <span
          className={cn(
            'text-xs px-2 py-0.5 rounded-full',
            profile.loop_detected ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700',
          )}
        >
          {profile.loop_detected ? 'Loop detected' : 'No loops'}
        </span>
        {profile.redundant_steps > 0 && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-gray-200 text-gray-600">
            {profile.redundant_steps} redundant
          </span>
        )}
      </div>

      {data.length >= 3 && (
        <div style={{ width: '100%', height: 260 }}>
          <ResponsiveContainer>
            <RadarChart data={data} outerRadius="70%">
              <PolarGrid />
              <PolarAngleAxis dataKey="axis" tick={{ fontSize: 11, fill: '#6b7280' }} />
              <PolarRadiusAxis domain={[0, 10]} tick={{ fontSize: 10, fill: '#9ca3af' }} />
              <Radar name="Score" dataKey="score" stroke="#0891b2" fill="#06b6d4" fillOpacity={0.5} />
              <Tooltip />
            </RadarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Per-axis scores + reasons */}
      <div className="space-y-1.5">
        {profile.axes.map((a) => (
          <div key={a.key} className="text-xs">
            <div className="flex items-center justify-between">
              <span className="text-gray-700">{a.name}</span>
              <span className="font-medium">{a.score}/10</span>
            </div>
            {a.reason && <p className="text-gray-500">{a.reason}</p>}
          </div>
        ))}
      </div>

      {profile.summary && (
        <p className="text-xs text-gray-600 italic border-t pt-2">{profile.summary}</p>
      )}

      {/* Evidence bank: facts accumulated step by step */}
      {profile.evidence_bank.length > 0 && (
        <div className="border-t pt-2 space-y-1.5">
          <p className="text-xs font-medium text-gray-600">Evidence bank ({profile.evidence_bank.length} steps)</p>
          {profile.evidence_bank.map((s) => (
            <div key={s.seq} className="text-xs border rounded px-2 py-1 bg-white">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="font-medium text-gray-700">
                  #{s.seq} {s.tool_name ? `${s.kind}/${s.tool_name}` : s.kind}
                </span>
                {s.redundant && (
                  <span className="px-1 rounded bg-amber-100 text-amber-700">redundant</span>
                )}
                <span
                  className={cn(
                    'px-1 rounded',
                    s.grounded ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700',
                  )}
                >
                  {s.grounded ? 'grounded' : 'ungrounded'}
                </span>
                {s.error && <span className="px-1 rounded bg-red-100 text-red-700">error</span>}
              </div>
              {s.facts.length > 0 && (
                <ul className="mt-1 list-disc list-inside text-gray-500">
                  {s.facts.map((f, i) => (
                    <li key={i}>{f}</li>
                  ))}
                </ul>
              )}
              {s.note && <p className="text-gray-400 mt-0.5">{s.note}</p>}
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
        <span>Judge: {profile.judge_model}</span>
        <span>{profile.judge_calls} calls</span>
        <span>${profile.judge_cost_usd.toFixed(4)}</span>
        <span>
          {profile.judge_input_tokens.toLocaleString()} in · {profile.judge_output_tokens.toLocaleString()} out
        </span>
        {profile.input_capped && (
          <span className="text-amber-600" title="trajectory was trimmed to fit the judge token/step budget">
            input capped
          </span>
        )}
      </div>
    </>
  )
}
