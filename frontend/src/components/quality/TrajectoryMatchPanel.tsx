import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { GitCompare, RefreshCw, AlertCircle, Save } from 'lucide-react'
import { qualityApi, tasksApi } from '@/api/client'
import type { TrajectoryMatchProfile, CanonicalTrajectory } from '@/types'
import { cn } from '@/lib/utils'

/** Trajectory Matching (E-09): deterministic, LLM-free comparison of the agent's
 *  actual tool-call sequence against a canonical (gold) trajectory. Only applies
 *  to tasks that have a canonical_trajectory; computes exact / edit / dag metrics. */

interface Props {
  taskId: string
}

/** Parse the textarea: JSON if it parses, otherwise comma/newline-separated tool
 *  names. Empty input clears the canonical trajectory. */
function parseInput(text: string): CanonicalTrajectory | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  try {
    return JSON.parse(trimmed)
  } catch {
    const list = trimmed
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    return list
  }
}

export default function TrajectoryMatchPanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const { data: profileData, isFetching } = useQuery({
    queryKey: ['trajectory-match-profile', taskId],
    queryFn: () => qualityApi.getTrajectoryMatch(taskId),
    enabled: open,
    retry: false,
  })
  const profile = profileData?.trajectory_match_profile ?? null

  const { data: task } = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => tasksApi.get(taskId),
    enabled: open,
  })
  const canonical = task?.canonical_trajectory ?? null
  // The editable text: the draft if the user is editing, else the stored value.
  const editorValue =
    draft ?? (canonical ? JSON.stringify(canonical) : '')

  const save = useMutation({
    mutationFn: (value: CanonicalTrajectory | null) =>
      tasksApi.update(taskId, { canonical_trajectory: value }),
    onSuccess: () => {
      setDraft(null)
      queryClient.invalidateQueries({ queryKey: ['task', taskId] })
    },
  })

  const evaluate = useMutation({
    mutationFn: () => qualityApi.evaluateTrajectoryMatch(taskId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['trajectory-match-profile', taskId] }),
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <GitCompare className="h-4 w-4" />
        Trajectory match
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Trajectory match (E-09)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {/* Canonical trajectory editor — normally set from a benchmark dataset (E-23). */}
      <div className="space-y-1">
        <label className="text-xs font-medium text-gray-600">Canonical trajectory</label>
        <textarea
          value={editorValue}
          onChange={(e) => setDraft(e.target.value)}
          rows={3}
          placeholder='e.g. search, write_file, run_tests  — or  {"nodes":[…],"edges":[…]}'
          className="w-full text-xs font-mono border rounded px-2 py-1.5 bg-white"
        />
        <div className="flex items-center gap-2">
          <button
            onClick={() => save.mutate(parseInput(editorValue))}
            disabled={save.isPending || draft === null}
            className="flex items-center gap-1.5 px-2.5 py-1 border rounded text-xs hover:bg-white disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" />
            {save.isPending ? 'Saving…' : 'Save canonical'}
          </button>
          <span className="text-xs text-gray-400">
            list of tool names, or a {`{nodes, edges}`} DAG (JSON)
          </span>
        </div>
        {save.isError && <p className="text-xs text-red-600">Save failed.</p>}
      </div>

      {isFetching && <p className="text-xs text-gray-400">Loading…</p>}
      {!isFetching && profile && <ProfileView profile={profile} />}
      {!isFetching && !profile && <p className="text-xs text-gray-400">Not yet matched.</p>}

      {evaluate.isError && <p className="text-xs text-red-600">Match request failed.</p>}
      {evaluate.data?.skipped && (
        <p className="text-xs text-amber-600">{evaluate.data.detail}</p>
      )}

      <button
        onClick={() => evaluate.mutate()}
        disabled={evaluate.isPending || !canonical}
        title={!canonical ? 'Set a canonical trajectory first' : undefined}
        className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-white disabled:opacity-50"
      >
        <RefreshCw className={cn('h-4 w-4', evaluate.isPending && 'animate-spin')} />
        {evaluate.isPending ? 'Matching…' : profile ? 'Re-match' : 'Match trajectory'}
      </button>
    </div>
  )
}

function ProfileView({ profile }: { profile: TrajectoryMatchProfile }) {
  if (profile.status === 'error') {
    return (
      <div className="flex items-start gap-2 text-xs text-red-600">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span>Match error: {profile.errors[0]?.error ?? 'unknown'}</span>
      </div>
    )
  }

  const len = Math.max(profile.actual_sequence.length, profile.reference_sequence.length)
  const rows = Array.from({ length: len }, (_, i) => ({
    i,
    actual: profile.actual_sequence[i],
    ref: profile.reference_sequence[i],
    same: profile.actual_sequence[i] === profile.reference_sequence[i],
  }))

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            'text-xs px-2 py-0.5 rounded-full',
            profile.matched ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700',
          )}
        >
          {profile.matched ? 'Match' : 'No match'}
        </span>
        <span className="text-sm">
          {profile.mode} score:{' '}
          <span className="font-medium text-gray-700">
            {profile.score != null ? profile.score.toFixed(2) : '—'}
          </span>
          {profile.threshold != null && profile.mode === 'edit' && (
            <span className="text-gray-400"> (≥ {profile.threshold})</span>
          )}
        </span>
        {profile.reference_form && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-gray-200 text-gray-600">
            {profile.reference_form}
          </span>
        )}
      </div>

      {/* The three metrics */}
      <div className="flex flex-wrap gap-2 text-xs">
        {(['exact', 'edit', 'dag'] as const).map((m) => (
          <span
            key={m}
            className={cn(
              'px-2 py-0.5 rounded border',
              m === profile.mode ? 'border-violet-300 bg-violet-50 text-violet-700' : 'border-gray-200 text-gray-600',
            )}
            title={m === profile.mode ? 'headline metric' : undefined}
          >
            {m}: {profile.metrics[m].toFixed(2)}
          </span>
        ))}
      </div>

      {/* Actual vs reference tool sequences, aligned by position */}
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div>
          <p className="font-medium text-gray-600 mb-1">Actual ({profile.actual_sequence.length})</p>
          <ol className="space-y-0.5">
            {rows.map((r) => (
              <li
                key={r.i}
                className={cn('font-mono px-1 rounded', r.actual && !r.same && 'bg-red-50 text-red-700')}
              >
                {r.actual ? `${r.i + 1}. ${r.actual}` : ''}
              </li>
            ))}
          </ol>
        </div>
        <div>
          <p className="font-medium text-gray-600 mb-1">Reference ({profile.reference_sequence.length})</p>
          <ol className="space-y-0.5">
            {rows.map((r) => (
              <li
                key={r.i}
                className={cn('font-mono px-1 rounded', r.ref && !r.same && 'bg-amber-50 text-amber-700')}
              >
                {r.ref ? `${r.i + 1}. ${r.ref}` : ''}
              </li>
            ))}
          </ol>
        </div>
      </div>

      {profile.detail && (
        <p className="text-xs text-gray-600 italic border-t pt-2">{profile.detail}</p>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
        <span>{profile.trace_stats.tool_steps ?? 0} tool steps</span>
        <span>{profile.trace_stats.steps_total ?? 0} total steps</span>
      </div>
    </>
  )
}
