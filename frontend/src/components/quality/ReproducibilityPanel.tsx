import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Camera, GitCompare, RefreshCw, Play } from 'lucide-react'
import { qualityApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { ExperimentSnapshot, SnapshotDiff } from '@/types'
import { cn } from '@/lib/utils'

type View = 'snapshot' | 'diff'

/** Reproducibility Snapshot (E-20): each eval run captures an experiment_snapshot
 *  (model · prompt · memory · tools · task input) into quality_records.reproducibility
 *  with a deterministic fingerprint. This per-task panel inspects one run, diffs two
 *  runs, or replays a run from its snapshot. Fields the runtime doesn't expose
 *  (temperature, tool versions, RAG vectors) are shown as missing, not faked. */
export default function ReproducibilityPanel() {
  const role = useAuth((s) => s.workspaces.find((w) => w.id === s.workspaceId)?.role ?? null)
  const isAdmin = role === 'owner' || role === 'admin'
  const [view, setView] = useState<View>('snapshot')

  return (
    <div className="bg-white rounded-lg border p-4 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <Camera className="h-5 w-5" />
          Reproducibility <span className="text-sm font-normal text-gray-400">(E-20)</span>
        </h2>
        <Segmented
          value={view}
          onChange={(v) => setView(v as View)}
          options={[
            { value: 'snapshot', label: 'Snapshot' },
            { value: 'diff', label: 'Diff' },
          ]}
        />
      </div>

      <p className="text-xs text-gray-500">
        Every eval run records the exact state that produced it — model, prompt, memory, tools and
        task input — with a fingerprint over the run-defining fields. Inspect one run, diff two, or
        replay from a snapshot. Fields the runtime can't expose (temperature, tool versions, RAG
        vectors) are listed as missing rather than faked.
      </p>

      {view === 'snapshot' ? <SnapshotView isAdmin={isAdmin} /> : <DiffView />}
    </div>
  )
}

function SnapshotView({ isAdmin }: { isAdmin: boolean }) {
  const queryClient = useQueryClient()
  const [input, setInput] = useState('')
  const [taskId, setTaskId] = useState('')
  const [replayMsg, setReplayMsg] = useState<string | null>(null)

  const snap = useQuery({
    queryKey: ['reproducibility', taskId],
    queryFn: () => qualityApi.getReproducibility(taskId),
    enabled: !!taskId,
    retry: false,
  })

  const capture = useMutation({
    mutationFn: () => qualityApi.captureReproducibility(taskId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['reproducibility', taskId] }),
  })

  const replay = useMutation({
    mutationFn: () => qualityApi.replayReproducibility(taskId),
    onSuccess: (r) => setReplayMsg(`Replay queued → task ${r.replay_task_id}`),
  })

  const reproducibility = snap.data?.reproducibility ?? null

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="task id"
          className="flex-1 min-w-[16rem] border rounded-lg px-3 py-1.5 text-sm font-mono"
        />
        <button
          onClick={() => {
            setReplayMsg(null)
            setTaskId(input.trim())
          }}
          className="px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
        >
          Load
        </button>
        {isAdmin && taskId && (
          <>
            <button
              onClick={() => capture.mutate()}
              disabled={capture.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              <RefreshCw className={cn('h-4 w-4', capture.isPending && 'animate-spin')} />
              Capture
            </button>
            <button
              onClick={() => replay.mutate()}
              disabled={replay.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              <Play className="h-4 w-4" />
              Replay
            </button>
          </>
        )}
      </div>

      {replayMsg && <p className="text-xs text-green-700">{replayMsg}</p>}
      {replay.isError && (
        <p className="text-xs text-red-600">Replay failed — needs a snapshot and owner/admin role.</p>
      )}
      {snap.isError && <p className="text-sm text-amber-700">No quality record for that task.</p>}
      {snap.isFetching && <p className="text-sm text-gray-400">Loading…</p>}
      {taskId && !snap.isFetching && !snap.isError && !reproducibility && (
        <p className="text-sm text-gray-400">
          No snapshot captured for this task yet{isAdmin ? ' — click Capture.' : '.'}
        </p>
      )}

      {reproducibility && <SnapshotDetail snap={reproducibility} />}
    </div>
  )
}

function SnapshotDetail({ snap }: { snap: ExperimentSnapshot }) {
  const d = snap.determinism
  const rows: [string, string][] = [
    ['Model', d.model_api_name ?? '—'],
    ['Temperature', d.temperature == null ? '—' : String(d.temperature)],
    ['Seed', d.seed == null ? '—' : String(d.seed)],
    ['Template', d.template_name ?? d.template_id ?? '—'],
    ['Tools', d.tools.length ? d.tools.join(', ') : '—'],
    ['Memory context', d.rag.memory_context_present ? 'captured' : '—'],
  ]
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-xs">
        <span className="text-gray-400">fingerprint</span>
        <code className="font-mono text-gray-700 break-all">{snap.fingerprint}</code>
      </div>

      <div className="flex flex-wrap gap-1">
        {snap.manifest.captured.map((c) => (
          <span
            key={c}
            className="px-2 py-0.5 rounded text-[11px] bg-green-50 text-green-700 border border-green-200"
          >
            {c}
          </span>
        ))}
        {snap.manifest.missing.map((m) => (
          <span
            key={m}
            title={snap.manifest.notes[m]}
            className="px-2 py-0.5 rounded text-[11px] bg-amber-50 text-amber-700 border border-amber-200"
          >
            missing: {m}
          </span>
        ))}
      </div>

      <table className="w-full text-xs">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k} className="border-b last:border-0">
              <td className="py-1 pr-3 text-gray-400 w-32 align-top">{k}</td>
              <td className="py-1 text-gray-700 break-all">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DiffView() {
  const [a, setA] = useState('')
  const [b, setB] = useState('')
  const diff = useMutation({
    mutationFn: () => qualityApi.diffReproducibility(a.trim(), b.trim()),
  })
  const d = diff.data as SnapshotDiff | undefined

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <input
          value={a}
          onChange={(e) => setA(e.target.value)}
          placeholder="task A id"
          className="flex-1 min-w-[12rem] border rounded-lg px-3 py-1.5 text-sm font-mono"
        />
        <input
          value={b}
          onChange={(e) => setB(e.target.value)}
          placeholder="task B id"
          className="flex-1 min-w-[12rem] border rounded-lg px-3 py-1.5 text-sm font-mono"
        />
        <button
          onClick={() => diff.mutate()}
          disabled={!a.trim() || !b.trim() || diff.isPending}
          className="flex items-center gap-1.5 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
        >
          <GitCompare className="h-4 w-4" />
          Compare
        </button>
      </div>

      {diff.isError && (
        <p className="text-sm text-amber-700">Both tasks must have a captured snapshot.</p>
      )}

      {d && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm">
            <span
              className={cn(
                'px-2 py-0.5 rounded text-xs border',
                d.identical
                  ? 'bg-green-50 text-green-700 border-green-200'
                  : 'bg-amber-50 text-amber-700 border-amber-200',
              )}
            >
              {d.identical ? 'identical' : 'changed'}
            </span>
            <span className="text-gray-600">{d.summary}</span>
          </div>

          {Object.keys(d.changed).length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-400 border-b">
                  <th className="py-1 pr-2 font-medium">Field</th>
                  <th className="py-1 px-2 font-medium">From</th>
                  <th className="py-1 pl-2 font-medium">To</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(d.changed).map(([k, v]) => (
                  <tr key={k} className="border-b last:border-0">
                    <td className="py-1 pr-2 font-medium text-gray-700 align-top">{k}</td>
                    <td className="py-1 px-2 text-gray-500 break-all">{fmtValue(v.from)}</td>
                    <td className="py-1 pl-2 text-gray-700 break-all">{fmtValue(v.to)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}

function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function Segmented({
  value,
  onChange,
  options,
}: {
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <div className="inline-flex rounded-lg border overflow-hidden text-xs">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'px-2.5 py-1.5 hover:bg-gray-50',
            value === o.value ? 'bg-gray-100 font-medium text-gray-800' : 'text-gray-500',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}
