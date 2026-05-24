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
import { Route, RefreshCw, AlertCircle } from 'lucide-react'
import { qualityApi } from '@/api/client'
import type { TrajectoryProfile } from '@/types'
import { cn } from '@/lib/utils'

/** 6-axis Trajectory Judge (E-07): scores HOW the agent reached its result
 *  (efficiency, tool selection, parameter quality, error recovery, goal
 *  alignment, loop detection). Reads the cleaned trace (E-06) as input. */

interface Props {
  taskId: string
}

export default function TrajectoryScorePanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data, isFetching } = useQuery({
    queryKey: ['trajectory-profile', taskId],
    queryFn: () => qualityApi.getTrajectoryProfile(taskId),
    enabled: open,
    retry: false,
  })
  const profile = data?.trajectory_profile ?? null

  const mutation = useMutation({
    mutationFn: () => qualityApi.evaluateTrajectory(taskId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trajectory-profile', taskId] }),
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <Route className="h-4 w-4" />
        Trajectory score
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Trajectory score (E-07)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {isFetching && <p className="text-xs text-gray-400">Loading…</p>}

      {!isFetching && profile && <ProfileView profile={profile} />}

      {!isFetching && !profile && (
        <p className="text-xs text-gray-400">Not yet judged.</p>
      )}

      {mutation.isError && (
        <p className="text-xs text-red-600">Evaluation request failed.</p>
      )}
      {mutation.data?.skipped && (
        <p className="text-xs text-amber-600">{mutation.data.detail}</p>
      )}

      <button
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-white disabled:opacity-50"
      >
        <RefreshCw className={cn('h-4 w-4', mutation.isPending && 'animate-spin')} />
        {mutation.isPending ? 'Judging…' : profile ? 'Re-evaluate' : 'Evaluate trajectory'}
      </button>
    </div>
  )
}

function ProfileView({ profile }: { profile: TrajectoryProfile }) {
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
      <div className="flex items-center justify-between">
        <span className="text-sm">
          Overall:{' '}
          <span className="font-medium text-gray-700">
            {profile.overall_score != null ? `${profile.overall_score}/10` : '—'}
          </span>
        </span>
        <span
          className={cn(
            'text-xs px-2 py-0.5 rounded-full',
            profile.loop_detected ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700',
          )}
        >
          {profile.loop_detected ? 'Loop detected' : 'No loops'}
        </span>
      </div>

      {data.length >= 3 && (
        <div style={{ width: '100%', height: 260 }}>
          <ResponsiveContainer>
            <RadarChart data={data} outerRadius="70%">
              <PolarGrid />
              <PolarAngleAxis dataKey="axis" tick={{ fontSize: 11, fill: '#6b7280' }} />
              <PolarRadiusAxis domain={[0, 10]} tick={{ fontSize: 10, fill: '#9ca3af' }} />
              <Radar name="Score" dataKey="score" stroke="#7c3aed" fill="#8b5cf6" fillOpacity={0.5} />
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

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
        <span>Judge: {profile.judge_model}</span>
        <span>${profile.judge_cost_usd.toFixed(4)}</span>
        <span>
          {profile.judge_input_tokens.toLocaleString()} in · {profile.judge_output_tokens.toLocaleString()} out
        </span>
        {profile.input_capped && (
          <span className="text-amber-600" title="cleaned trace was trimmed to fit the judge token budget">
            input capped
          </span>
        )}
      </div>
    </>
  )
}
