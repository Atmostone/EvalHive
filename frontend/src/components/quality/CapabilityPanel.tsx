import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck, RefreshCw, AlertCircle, Save } from 'lucide-react'
import { qualityApi, tasksApi } from '@/api/client'
import type { CapabilityProfile, CapabilitySpec, CapabilityClassification } from '@/types'
import { cn } from '@/lib/utils'

/** Capability-isolation Tests (E-13): deterministic, LLM-free Glass-Box check of
 *  whether the agent actually used the tool(s) the task cannot be solved without.
 *  Only applies to tasks that carry a capability_spec; classifies the run as
 *  genuine / cheated / failed_with_tool / failed_no_tool. */

interface Props {
  taskId: string
}

/** Parse the textarea: a JSON spec object if it parses, otherwise a comma/newline
 *  list of tool names → {required_tools}. Empty input clears the spec. */
function parseInput(text: string): CapabilitySpec | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  try {
    const obj = JSON.parse(trimmed)
    if (Array.isArray(obj)) return { required_tools: obj.map(String) }
    return obj as CapabilitySpec
  } catch {
    const list = trimmed.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean)
    return { required_tools: list }
  }
}

const CLASS_STYLE: Record<CapabilityClassification, { label: string; cls: string }> = {
  genuine: { label: 'Genuine', cls: 'bg-green-100 text-green-700' },
  cheated: { label: 'Cheated 🤷', cls: 'bg-red-100 text-red-700' },
  failed_with_tool: { label: 'Failed (with tool)', cls: 'bg-amber-100 text-amber-700' },
  failed_no_tool: { label: 'Failed (no tool)', cls: 'bg-gray-200 text-gray-600' },
}

export default function CapabilityPanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const { data: profileData, isFetching } = useQuery({
    queryKey: ['capability-profile', taskId],
    queryFn: () => qualityApi.getCapability(taskId),
    enabled: open,
    retry: false,
  })
  const profile = profileData?.capability_profile ?? null

  const { data: task } = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => tasksApi.get(taskId),
    enabled: open,
  })
  const spec = task?.capability_spec ?? null
  const editorValue = draft ?? (spec ? JSON.stringify(spec) : '')

  const save = useMutation({
    mutationFn: (value: CapabilitySpec | null) =>
      tasksApi.update(taskId, { capability_spec: value }),
    onSuccess: () => {
      setDraft(null)
      queryClient.invalidateQueries({ queryKey: ['task', taskId] })
    },
  })

  const evaluate = useMutation({
    mutationFn: () => qualityApi.evaluateCapability(taskId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['capability-profile', taskId] }),
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <ShieldCheck className="h-4 w-4" />
        Capability isolation
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Capability isolation (E-13)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {/* Capability spec editor — normally set from a benchmark dataset (E-23). */}
      <div className="space-y-1">
        <label className="text-xs font-medium text-gray-600">Capability spec</label>
        <textarea
          value={editorValue}
          onChange={(e) => setDraft(e.target.value)}
          rows={3}
          placeholder='e.g. web_search, fetch_url  — or  {"required_tools":["bash"],"category":"exact_compute","match":"all"}'
          className="w-full text-xs font-mono border rounded px-2 py-1.5 bg-white"
        />
        <div className="flex items-center gap-2">
          <button
            onClick={() => save.mutate(parseInput(editorValue))}
            disabled={save.isPending || draft === null}
            className="flex items-center gap-1.5 px-2.5 py-1 border rounded text-xs hover:bg-white disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" />
            {save.isPending ? 'Saving…' : 'Save spec'}
          </button>
          <span className="text-xs text-gray-400">
            list of required tool names, or a {`{required_tools, category, match}`} object (JSON)
          </span>
        </div>
        {save.isError && <p className="text-xs text-red-600">Save failed.</p>}
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
        disabled={evaluate.isPending || !spec}
        title={!spec ? 'Set a capability spec first' : undefined}
        className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-white disabled:opacity-50"
      >
        <RefreshCw className={cn('h-4 w-4', evaluate.isPending && 'animate-spin')} />
        {evaluate.isPending ? 'Evaluating…' : profile ? 'Re-evaluate' : 'Evaluate capability'}
      </button>
    </div>
  )
}

function ProfileView({ profile }: { profile: CapabilityProfile }) {
  if (profile.status === 'error') {
    return (
      <div className="flex items-start gap-2 text-xs text-red-600">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span>Capability error: {profile.errors[0]?.error ?? 'unknown'}</span>
      </div>
    )
  }

  const style = profile.classification ? CLASS_STYLE[profile.classification] : null

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        {style && (
          <span className={cn('text-xs px-2 py-0.5 rounded-full font-medium', style.cls)}>
            {style.label}
          </span>
        )}
        <span
          className={cn(
            'text-xs px-2 py-0.5 rounded-full',
            profile.tool_used ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700',
          )}
        >
          {profile.tool_used ? 'tool used' : 'tool NOT used'}
        </span>
        <span
          className={cn(
            'text-xs px-2 py-0.5 rounded-full',
            profile.outcome_correct ? 'bg-green-100 text-green-700' : 'bg-gray-200 text-gray-600',
          )}
        >
          {profile.outcome_correct ? 'outcome correct' : 'outcome incorrect'}
        </span>
        {profile.category && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-gray-200 text-gray-600">
            {profile.category}
          </span>
        )}
      </div>

      {/* Required vs called tools */}
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div>
          <p className="font-medium text-gray-600 mb-1">
            Required ({profile.match}) — {profile.required_tools.length}
          </p>
          <ul className="space-y-0.5">
            {profile.required_tools.map((t) => (
              <li
                key={t}
                className={cn(
                  'font-mono px-1 rounded',
                  (profile.missing_tools ?? []).includes(t) && 'bg-red-50 text-red-700',
                )}
              >
                {t}
                {(profile.missing_tools ?? []).includes(t) && ' (missing)'}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <p className="font-medium text-gray-600 mb-1">
            Called ({(profile.tools_called ?? []).length})
          </p>
          <ul className="space-y-0.5">
            {(profile.tools_called ?? []).map((t) => (
              <li key={t} className="font-mono px-1 rounded text-gray-700">
                {t}
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 border-t pt-2">
        <span>
          outcome:{' '}
          <span className="text-gray-700">
            {profile.outcome_score != null ? profile.outcome_score.toFixed(2) : '—'}
          </span>
          {profile.outcome_threshold != null && (
            <span className="text-gray-400"> (≥ {profile.outcome_threshold})</span>
          )}
          {profile.outcome_signal && <span className="text-gray-400"> · {profile.outcome_signal}</span>}
        </span>
        <span>{profile.trace_stats?.tool_steps ?? 0} tool steps</span>
        <span>{profile.trace_stats?.steps_total ?? 0} total steps</span>
      </div>
    </>
  )
}
