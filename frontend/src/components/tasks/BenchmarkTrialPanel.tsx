import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { attemptsApi, benchmarkSdkApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { AttemptEvent } from '@/types'
import type { BenchmarkJournalEvent, BenchmarkTrial } from '@/types/benchmarks'
import BenchmarkRegrades from './BenchmarkRegrades'
import BenchmarkAggregates from './BenchmarkAggregates'

function AgentOutput({ taskId, active }: { taskId: string; active: boolean }) {
  const attempts = useQuery({ queryKey: ['task-attempts', taskId], queryFn: () => attemptsApi.list(taskId), refetchInterval: active ? 2000 : false })
  const attempt = attempts.data?.[0]
  return attempt ? <AttemptOutput key={attempt.id} id={attempt.id} active={active} /> : null
}

function AttemptOutput({ id, active }: { id: string; active: boolean }) {
  const [cursor, setCursor] = useState(-1)
  const [rows, setRows] = useState<AttemptEvent[]>([])
  const events = useQuery({ queryKey: ['benchmark-agent-output', id, cursor], queryFn: () => attemptsApi.output(id, cursor), gcTime: 0, refetchInterval: active ? 1000 : false })
  useEffect(() => {
    if (!events.data?.length) return
    setRows(previous => [...new Map([...previous, ...events.data!].map(row => [row.seq, row])).values()].slice(-200))
    setCursor(events.data[events.data.length - 1].seq)
  }, [events.data])
  return <details open className="space-y-2">
    <summary className="text-sm font-medium cursor-pointer">Agent output · latest 200 events</summary>
    {events.error && <p role="alert" className="text-sm text-red-600">{events.error.message}</p>}
    <div className="max-h-72 overflow-auto space-y-2 text-xs">
      {rows.map(row => <div key={row.seq}><span className="text-gray-500">#{row.seq} · {row.event}</span>
        <pre className="whitespace-pre-wrap break-words">{JSON.stringify(row.data, null, 2).slice(0, 8000)}</pre>
      </div>)}
    </div>
  </details>
}

function TrialDetails({ initial }: { initial: BenchmarkTrial }) {
  const queries = useQueryClient()
  const navigate = useNavigate()
  const [retryRequestId] = useState(() => crypto.randomUUID())
  const role = useAuth(state => state.workspaces.find(row => row.id === state.workspaceId)?.role)
  const writable = ['owner', 'admin', 'member'].includes(role ?? '')
  const current = useQuery({ queryKey: ['benchmark-trial', initial.id], queryFn: () => benchmarkSdkApi.trial(initial.id), refetchInterval: query => query.state.data?.phase === 'finished' ? false : 2000 })
  const trial = current.data ?? initial
  const active = trial.phase !== 'finished'
  const [cursor, setCursor] = useState(-1)
  const [journal, setJournal] = useState<BenchmarkJournalEvent[]>([])
  const [artifactOffset, setArtifactOffset] = useState(0)
  const [evaluationOffset, setEvaluationOffset] = useState(0)
  const events = useQuery({ queryKey: ['benchmark-journal', trial.id, cursor], queryFn: () => benchmarkSdkApi.journal(trial.id, cursor), gcTime: 0, refetchInterval: active ? 2000 : false })
  useEffect(() => {
    if (!events.data?.events.length) return
    setJournal(previous => [...new Map([...previous, ...events.data!.events].map(row => [row.seq, row])).values()].slice(-200))
    setCursor(events.data.next_seq)
  }, [events.data])
  useEffect(() => {
    if (!active) {
      queries.invalidateQueries({ queryKey: ['tasks'] })
      queries.invalidateQueries({ queryKey: ['task', trial.task_id] })
      queries.invalidateQueries({ queryKey: ['benchmark-evaluations', trial.id] })
      queries.invalidateQueries({ queryKey: ['benchmark-aggregates', trial.id] })
      queries.invalidateQueries({ queryKey: ['benchmark-artifacts', trial.id] })
      queries.invalidateQueries({ queryKey: ['benchmark-journal', trial.id] })
      queries.invalidateQueries({ queryKey: ['task-attempts', trial.task_id] })
      queries.invalidateQueries({ queryKey: ['benchmark-agent-output'] })
    }
  }, [active, trial.id, trial.task_id, queries])
  const evaluations = useQuery({ queryKey: ['benchmark-evaluations', trial.id, evaluationOffset], queryFn: () => benchmarkSdkApi.evaluations(trial.id, evaluationOffset), refetchInterval: active ? 2000 : false })
  const artifacts = useQuery({ queryKey: ['benchmark-artifacts', trial.id, artifactOffset], queryFn: () => benchmarkSdkApi.artifacts(trial.id, artifactOffset), refetchInterval: active ? 2000 : false })
  const cancel = useMutation({ mutationFn: () => benchmarkSdkApi.cancel(trial.id), onSuccess: () => queries.invalidateQueries({ queryKey: ['benchmark-trial', trial.id] }) })
  const retry = useMutation({ mutationFn: () => benchmarkSdkApi.retryTrial(trial.id, retryRequestId), onSuccess: result => {
    queries.invalidateQueries({ queryKey: ['tasks'] })
    if (result.task_id) navigate('/tasks?task=' + encodeURIComponent(result.task_id))
  } })
  const download = useMutation({ mutationFn: async (id: string) => {
    const blob = await benchmarkSdkApi.download(trial.id, id)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a'); link.href = url; link.download = id + '.bin'; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } })
  const error = current.error ?? events.error ?? evaluations.error ?? artifacts.error ?? cancel.error ?? retry.error ?? download.error
  return <section aria-label="Benchmark trial" className="border rounded-lg p-3 space-y-3">
    <h3 className="font-medium text-sm">Native benchmark · {trial.phase}</h3>
    <dl className="grid grid-cols-2 gap-2 text-xs">
      {Object.entries({ Execution: trial.execution_status, Evaluation: trial.evaluation_status, Recording: trial.recording_status, Cleanup: trial.cleanup_status }).map(([name, status]) =>
        <div key={name}><dt className="text-gray-500">{name}</dt><dd>{status}</dd></div>)}
    </dl>
    {active && writable && <button type="button" disabled={cancel.isPending || !!trial.cancel_requested_at}
      onClick={() => cancel.mutate()} className="text-sm text-red-600 disabled:opacity-50">{trial.cancel_requested_at ? 'Cancellation requested' : 'Cancel benchmark run'}</button>}
    {!active && writable && trial.cleanup_status === 'complete' && <button title="Create a standalone Work execution; prior results and matrix cells stay unchanged." disabled={retry.isPending} onClick={() => retry.mutate()} className="text-sm text-blue-600 disabled:opacity-50">Repeat with recorded settings</button>}
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    <div className="space-y-2" aria-label="Native evaluations">
      <h4 className="font-medium text-sm">Native evaluation history</h4>
      {evaluations.data?.map(row => <div key={row.id} className="border rounded p-2 text-xs space-y-1">
        <p>{row.kind} · checker: {row.status}</p>
        <p>Task correctness: {row.success === null ? 'unknown' : row.success ? 'correct' : 'incorrect'}{row.verdict ? ` · ${row.verdict}` : ''}</p>
        {row.error_kind && <p className="text-amber-700">{row.error_kind}</p>}
        {row.metrics?.map((metric, index) => <p key={index}>{metric.name}: {typeof metric.value === 'string' ? metric.value : JSON.stringify(metric.value)}{metric.aggregation ? ` · ${metric.aggregation}` : ''}</p>)}
      </div>)}
      {!evaluations.data?.length && <p className="text-xs text-gray-500">No native verdict recorded.</p>}
      <div className="flex gap-3 text-xs">
        {evaluationOffset > 0 && <button onClick={() => setEvaluationOffset(value => Math.max(0, value - 100))}>Previous evaluations</button>}
        {evaluations.data?.length === 100 && <button onClick={() => setEvaluationOffset(value => value + 100)}>Next evaluations</button>}
      </div>
    </div>
    <BenchmarkRegrades trial={trial} evaluations={evaluations.data ?? []} writable={writable} />
    <BenchmarkAggregates trialId={trial.id} active={active} />
    <div className="space-y-2">
      <h4 className="font-medium text-sm">Files</h4>
      {artifacts.data?.map(row => <button key={row.id} type="button" disabled={download.isPending} onClick={() => download.mutate(row.id)}
        className="block text-xs text-blue-600 disabled:opacity-50">{row.artifact.id} · {row.artifact.size_bytes.toLocaleString()} bytes</button>)}
      {!artifacts.data?.length && <p className="text-xs text-gray-500">No public files retained.</p>}
      <div className="flex gap-3 text-xs">
        {artifactOffset > 0 && <button onClick={() => setArtifactOffset(value => Math.max(0, value - 100))}>Previous files</button>}
        {artifacts.data?.length === 100 && <button onClick={() => setArtifactOffset(value => value + 100)}>Next files</button>}
      </div>
    </div>
    {trial.task_id && <AgentOutput taskId={trial.task_id} active={active} />}
    <details><summary className="text-xs cursor-pointer">Lifecycle journal · latest 200 events</summary>
      <div className="max-h-48 overflow-auto text-xs space-y-1">{journal.map(row => <p key={row.seq}>#{row.seq} · {row.event} · {JSON.stringify(row.data)}</p>)}</div>
    </details>
  </section>
}

export default function BenchmarkTrialPanel({ taskId }: { taskId: string }) {
  const trials = useQuery({ queryKey: ['benchmark-task-trials', taskId], queryFn: () => benchmarkSdkApi.trials(taskId) })
  if (trials.error) return <p role="alert" className="text-sm text-red-600">{trials.error.message}</p>
  return <>{trials.data?.map(trial => <TrialDetails key={trial.id} initial={trial} />)}</>
}
