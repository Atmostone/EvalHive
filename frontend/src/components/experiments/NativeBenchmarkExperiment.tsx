import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { experimentsApi, harnessesApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { ExperimentDetail, ExperimentRunResult } from '@/types'
import BenchmarkTrialPanel from '@/components/tasks/BenchmarkTrialPanel'

function CellHistory({ experimentId, cellId }: { experimentId: string; cellId: string }) {
  const [offset, setOffset] = useState(0)
  const history = useQuery({ queryKey: ['native-cell-history', experimentId, cellId, offset], queryFn: () => experimentsApi.cellAttempts(experimentId, cellId, offset) })
  return <section aria-label="Previous cell attempts" className="text-xs space-y-2">
    <h4 className="font-medium">Previous attempts</h4>
    {history.error && <p role="alert" className="text-red-600">{history.error.message}</p>}
    {history.data?.map(attempt => <div key={attempt.id}>
      {attempt.task_available && attempt.task_id ? <Link className="text-blue-600" to={'/tasks?task=' + encodeURIComponent(attempt.task_id)}>Attempt {attempt.attempt_index} · {attempt.status}</Link> : <p>Attempt {attempt.attempt_index} · {attempt.status} · execution record unavailable</p>}
      <p>Native correctness: {attempt.external_verdict === null ? 'unknown' : attempt.external_verdict ? 'correct' : 'incorrect'} · cost: {attempt.cost_usd === null ? 'unknown' : `$${Number(attempt.cost_usd).toFixed(4)}`}</p>
    </div>)}
    {offset > 0 && <button onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous history page</button>}
    {history.data?.length === 100 && <button onClick={() => setOffset(value => value + 100)}>Next history page</button>}
  </section>
}

function NativeRun({ row, label, experimentId }: { row: ExperimentRunResult; label: string; experimentId: string }) {
  const [open, setOpen] = useState(false)
  return <details className="border rounded p-3" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer text-sm">{label} · {row.case_key} · repeat {row.run_index + 1} · {row.status}</summary>
    <div className="space-y-3 mt-3">
      <p className="text-xs">Known run cost: {row.cost_usd == null ? 'unknown' : `$${Number(row.cost_usd).toFixed(4)}`}</p>
      {row.task_id && <Link to={'/tasks?task=' + encodeURIComponent(row.task_id)} className="block text-xs text-blue-600">Open execution history</Link>}
      {open && row.task_id && <BenchmarkTrialPanel taskId={row.task_id} />}
      {!row.task_id && <p className="text-xs text-gray-500">This cell has not started.</p>}
      {open && row.id && <CellHistory experimentId={experimentId} cellId={row.id} />}
    </div>
  </details>
}

function MatrixChanges({ detail, refresh }: { detail: ExperimentDetail; refresh: () => void }) {
  const navigate = useNavigate()
  const [title, setTitle] = useState(`${detail.name} (copy)`)
  const [repeats, setRepeats] = useState(detail.n_runs_per_cell)
  const [current, setCurrent] = useState(false)
  const [profile, setProfile] = useState('')
  const profiles = useQuery({ queryKey: ['harness-profiles'], queryFn: harnessesApi.profiles })
  const clone = useMutation({ mutationFn: () => experimentsApi.clone(detail.id, {
    name: title.trim(), changes: { n_runs_per_cell: repeats, ...(current ? {
      configurations: detail.configurations.filter(config => !config.retired_at).map(config => Object.fromEntries(
        Object.entries(config).filter(([key]) => ['label', 'harness_profile_id', 'model_id', 'harness_config', 'benchmark_config', 'limits', 'agent_max_turns'].includes(key)),
      )),
    } : {}) },
  }), onSuccess: result => navigate(`/experiments/${result.id}`) })
  const add = useMutation({ mutationFn: () => experimentsApi.addConfig(detail.id, { harness_profile_id: profile }), onSuccess: () => { setProfile(''); refresh() } })
  const retire = useMutation({ mutationFn: (key: string) => experimentsApi.retireConfig(detail.id, key), onSuccess: refresh })
  const error = clone.error ?? add.error ?? retire.error ?? profiles.error
  const editable = detail.status !== 'running' && !(detail.status === 'cancelled' && !detail.completed_at)
  return <section className="space-y-3 text-sm border rounded p-3" aria-label="Native matrix changes">
    <details><summary className="cursor-pointer">Clone matrix</summary><div className="space-y-2 mt-2">
      <label className="block">Copy name <input aria-label="Native clone name" maxLength={255} value={title} onChange={event => setTitle(event.target.value)} className="border rounded px-2 py-1" /></label>
      <label className="block">Repetitions <input aria-label="Native clone repetitions" type="number" min={1} max={20} value={repeats} onChange={event => setRepeats(Number(event.target.value))} className="border rounded px-2 py-1" /></label>
      <label className="block"><input type="checkbox" checked={current} onChange={event => setCurrent(event.target.checked)} /> Resolve current profile settings as new conditions</label>
      <p className="text-xs text-gray-500">{current ? 'Creates a draft using current profiles and records new conditions.' : 'Copies recorded settings and pinned versions. Later profile edits are ignored.'} Retired configurations are excluded.</p>
      <button disabled={clone.isPending || !title.trim() || !Number.isInteger(repeats) || repeats < 1 || repeats > 20} onClick={() => clone.mutate()} className="text-blue-600 disabled:opacity-50">Create matrix copy</button>
    </div></details>
    <details><summary className="cursor-pointer">Configurations and history</summary><div className="space-y-2 mt-2">
      <p className="text-xs text-gray-500">Pause new runs and wait for active execution to finish before changing configurations. Retiring preserves execution history.</p>
      {detail.configurations.map(config => <div key={config.config_key} className="flex gap-3 items-center">
        <span>{config.label} · {config.retired_at ? 'retired' : config.config_key}</span>
        {!config.retired_at && <button disabled={!editable || detail.n_configs <= 1 || retire.isPending} onClick={() => retire.mutate(config.config_key)} className="text-red-600 disabled:opacity-50">Retire {config.label}</button>}
      </div>)}
      <label className="block">Add current harness profile <select aria-label="Add native matrix profile" value={profile} onChange={event => setProfile(event.target.value)} className="border rounded p-1">
        <option value="">Select profile</option>{profiles.data?.map(row => <option key={row.id} value={row.id}>{row.name} · {row.connector_id}</option>)}
      </select></label>
      <button disabled={!editable || !profile || add.isPending || detail.n_configs >= 24} onClick={() => add.mutate()} className="text-blue-600 disabled:opacity-50">Add configuration</button>
    </div></details>
    {error && <p role="alert" className="text-red-600">{error.message}</p>}
  </section>
}

export default function NativeBenchmarkExperiment({ detail }: { detail: ExperimentDetail }) {
  const queries = useQueryClient()
  const role = useAuth(state => state.workspaces.find(row => row.id === state.workspaceId)?.role)
  const writable = ['owner', 'admin'].includes(role ?? '')
  const refresh = () => { queries.invalidateQueries({ queryKey: ['experiment', detail.id] }); queries.invalidateQueries({ queryKey: ['native-experiment-results', detail.id] }); queries.invalidateQueries({ queryKey: ['native-cell-history', detail.id] }) }
  const control = useMutation({ mutationFn: (action: 'run' | 'pause' | 'resume' | 'cancel') => experimentsApi[action](detail.id), onSuccess: refresh })
  const retryFailed = useMutation({ mutationFn: () => experimentsApi.retryFailed(detail.id), onSuccess: refresh })
  const [includeRetired, setIncludeRetired] = useState(false)
  const active = ['running', 'paused'].includes(detail.status) || (detail.status === 'cancelled' && !detail.completed_at)
  const results = useQuery({ queryKey: ['native-experiment-results', detail.id, includeRetired], queryFn: () => experimentsApi.results(detail.id, { includeRetired }), refetchInterval: active ? 2000 : false })
  const labels = Object.fromEntries(detail.configurations.map(config => [config.config_key, config.label]))
  const error = control.error ?? retryFailed.error ?? results.error
  return <div className="p-6 space-y-4">
    <Link to="/experiments" className="text-sm text-blue-600">← Experiments</Link>
    <div><h1 className="text-2xl font-semibold">{detail.name}</h1><p className="text-sm text-gray-500">Native benchmark matrix · {detail.status}</p></div>
    {detail.cloned_from_id && <p className="text-sm">Copied from <Link className="text-blue-600" to={`/experiments/${detail.cloned_from_id}`}>matrix revision {detail.cloned_from_revision}</Link> · {detail.clone_mode === 'frozen' ? 'recorded conditions' : 'new conditions'}</p>}
    <p className="text-sm">{detail.n_configs} configurations × {detail.n_cases} cases × {detail.n_runs_per_cell} repetitions = {detail.total_runs} runs</p>
    {writable && <div className="flex gap-3 text-sm">
      {detail.status === 'draft' && <button disabled={control.isPending} onClick={() => control.mutate('run')} className="text-blue-600">Start native matrix</button>}
      {detail.status === 'running' && <button disabled={control.isPending} onClick={() => control.mutate('pause')}>Pause new runs</button>}
      {detail.status === 'paused' && <button disabled={control.isPending} onClick={() => control.mutate('resume')}>Resume native matrix</button>}
      {['draft', 'running', 'paused'].includes(detail.status) && <button disabled={control.isPending} onClick={() => control.mutate('cancel')} className="text-red-600">Cancel native matrix</button>}
      {['completed', 'failed', 'capped', 'cancelled', 'paused'].includes(detail.status) && results.data?.some(row => row.status === 'failed') && <button disabled={retryFailed.isPending} onClick={() => retryFailed.mutate()} className="text-blue-600">Retry failed cells</button>}
    </div>}
    {writable && <MatrixChanges key={detail.id} detail={detail} refresh={refresh} />}
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    {detail.error && <p className="text-sm text-amber-700">{detail.error}</p>}
    <p className="text-xs text-gray-500">Each cell retains its native evaluation and metrics. Execution completion and task correctness are shown separately. Unknown measurements remain unknown.</p>
    {!!detail.n_retired_configs && <label className="block text-sm"><input type="checkbox" checked={includeRetired} onChange={event => setIncludeRetired(event.target.checked)} /> Include retired execution history</label>}
    <div className="space-y-2">{results.data?.map(row => <NativeRun key={`${row.config_key}:${row.case_key}:${row.run_index}`} row={row} experimentId={detail.id} label={labels[row.config_key] ?? row.config_key} />)}</div>
    {!results.data?.length && <p className="text-sm text-gray-500">Start the draft to create its matrix cells.</p>}
  </div>
}
