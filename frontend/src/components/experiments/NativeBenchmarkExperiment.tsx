import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { experimentsApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { ExperimentDetail, ExperimentRunResult } from '@/types'
import BenchmarkTrialPanel from '@/components/tasks/BenchmarkTrialPanel'

function NativeRun({ row, label }: { row: ExperimentRunResult; label: string }) {
  const [open, setOpen] = useState(false)
  return <details className="border rounded p-3" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer text-sm">{label} · {row.case_key} · repeat {row.run_index + 1} · {row.status}</summary>
    <div className="space-y-3 mt-3">
      <p className="text-xs">Known run cost: {row.cost_usd == null ? 'unknown' : `$${Number(row.cost_usd).toFixed(4)}`}</p>
      {open && row.task_id && <BenchmarkTrialPanel taskId={row.task_id} />}
      {!row.task_id && <p className="text-xs text-gray-500">This cell has not started.</p>}
    </div>
  </details>
}

export default function NativeBenchmarkExperiment({ detail }: { detail: ExperimentDetail }) {
  const queries = useQueryClient()
  const role = useAuth(state => state.workspaces.find(row => row.id === state.workspaceId)?.role)
  const writable = ['owner', 'admin'].includes(role ?? '')
  const refresh = () => { queries.invalidateQueries({ queryKey: ['experiment', detail.id] }); queries.invalidateQueries({ queryKey: ['native-experiment-results', detail.id] }) }
  const control = useMutation({ mutationFn: (action: 'run' | 'pause' | 'resume' | 'cancel') => experimentsApi[action](detail.id), onSuccess: refresh })
  const active = ['running', 'paused'].includes(detail.status) || (detail.status === 'cancelled' && !detail.completed_at)
  const results = useQuery({ queryKey: ['native-experiment-results', detail.id], queryFn: () => experimentsApi.results(detail.id), refetchInterval: active ? 2000 : false })
  const labels = Object.fromEntries(detail.configurations.map(config => [config.config_key, config.label]))
  const error = control.error ?? results.error
  return <div className="p-6 space-y-4">
    <Link to="/experiments" className="text-sm text-blue-600">← Experiments</Link>
    <div><h1 className="text-2xl font-semibold">{detail.name}</h1><p className="text-sm text-gray-500">Native benchmark matrix · {detail.status}</p></div>
    <p className="text-sm">{detail.n_configs} configurations × {detail.n_cases} cases × {detail.n_runs_per_cell} repetitions = {detail.total_runs} runs</p>
    {writable && <div className="flex gap-3 text-sm">
      {detail.status === 'draft' && <button disabled={control.isPending} onClick={() => control.mutate('run')} className="text-blue-600">Start native matrix</button>}
      {detail.status === 'running' && <button disabled={control.isPending} onClick={() => control.mutate('pause')}>Pause new runs</button>}
      {detail.status === 'paused' && <button disabled={control.isPending} onClick={() => control.mutate('resume')}>Resume native matrix</button>}
      {['draft', 'running', 'paused'].includes(detail.status) && <button disabled={control.isPending} onClick={() => control.mutate('cancel')} className="text-red-600">Cancel native matrix</button>}
    </div>}
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    {detail.error && <p className="text-sm text-amber-700">{detail.error}</p>}
    <p className="text-xs text-gray-500">Each cell retains its native evaluation and metrics. Execution completion and task correctness are shown separately. Unknown measurements remain unknown.</p>
    <div className="space-y-2">{results.data?.map(row => <NativeRun key={`${row.config_key}:${row.case_key}:${row.run_index}`} row={row} label={labels[row.config_key] ?? row.config_key} />)}</div>
    {!results.data?.length && <p className="text-sm text-gray-500">Start the draft to create its matrix cells.</p>}
  </div>
}
