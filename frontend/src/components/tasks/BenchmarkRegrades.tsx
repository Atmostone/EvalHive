import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { benchmarkSdkApi } from '@/api/client'
import type { BenchmarkEvaluation, BenchmarkEvaluationJob, BenchmarkTrial } from '@/types/benchmarks'

const pending = (job: BenchmarkEvaluationJob) => ['queued', 'running'].includes(job.status)

export default function BenchmarkRegrades({ trial, evaluations, writable }: {
  trial: BenchmarkTrial; evaluations: BenchmarkEvaluation[]; writable: boolean
}) {
  const queries = useQueryClient()
  const [selected, setSelected] = useState('')
  const [offset, setOffset] = useState(0)
  const [requests, setRequests] = useState<Record<string, string>>({})
  const candidates = evaluations.filter(row => row.status !== 'running')
  const source = candidates.some(row => row.id === selected) ? selected : candidates[candidates.length - 1]?.id ?? ''
  const jobs = useQuery({
    queryKey: ['benchmark-evaluation-jobs', trial.id, offset],
    queryFn: () => benchmarkSdkApi.evaluationJobs(trial.id, offset),
    refetchInterval: query => query.state.data?.some(pending) ? 2000 : false,
  })
  const refresh = () => queries.invalidateQueries({ queryKey: ['benchmark-evaluation-jobs', trial.id] })
  const regrade = useMutation({
    mutationFn: (input: { source: string; requestId: string }) => benchmarkSdkApi.regrade(trial.id, input.source, input.requestId),
    onSuccess: (_, input) => setRequests(previous => {
      const next = { ...previous }; delete next[input.source]; return next
    }),
    onSettled: refresh,
  })
  const cancel = useMutation({
    mutationFn: (id: string) => benchmarkSdkApi.cancelEvaluationJob(trial.id, id),
    onSettled: refresh,
  })
  useEffect(() => {
    if (!jobs.data) return
    queries.invalidateQueries({ queryKey: ['benchmark-evaluations', trial.id] })
    queries.invalidateQueries({ queryKey: ['benchmark-aggregates', trial.id] })
    const acknowledged = jobs.data.filter(job => requests[job.source_evaluation_id] === job.request_id)
    if (acknowledged.length) {
      setRequests(previous => {
        const next = { ...previous }
        acknowledged.forEach(job => delete next[job.source_evaluation_id])
        return next
      })
      regrade.reset()
    }
  }, [jobs.data, requests, queries, trial.id, regrade.reset])
  const submit = () => {
    const requestId = requests[source] ?? crypto.randomUUID()
    setRequests(previous => ({ ...previous, [source]: requestId }))
    regrade.mutate({ source, requestId })
  }
  const error = jobs.error ?? regrade.error ?? cancel.error
  return <section aria-label="Saved-state reevaluation" className="space-y-2 border rounded p-2">
    <h4 className="text-sm font-medium">Re-evaluate saved state</h4>
    <p className="text-xs text-gray-500">Creates a separate verdict from retained evidence. The original execution and verdict stay in history.</p>
    {writable && trial.phase === 'finished' && <div className="flex gap-2 flex-wrap text-xs">
      <select aria-label="Source native evaluation" value={source} onChange={event => setSelected(event.target.value)} className="border rounded p-1">
        {!candidates.length && <option value="">No saved evaluation</option>}
        {candidates.map(row => <option key={row.id} value={row.id}>{row.kind} · {row.started_at ?? row.id} · {row.status}</option>)}
      </select>
      <button disabled={!source || regrade.isPending || jobs.data?.some(pending)} onClick={submit} className="text-blue-600 disabled:opacity-50">Regrade saved state</button>
    </div>}
    {error && <p role="alert" className="text-xs text-red-600">{error.message}</p>}
    {jobs.data?.map(job => <div key={job.id} className="text-xs space-y-1">
      <p>Regrade · {job.status} · {job.phase}{job.cleanup_status ? ` · cleanup: ${job.cleanup_status}` : ''}</p>
      {job.error_code && <p className="text-amber-700">{job.error_code}</p>}
      {writable && pending(job) && <button aria-label={`Cancel regrade ${job.id}`} disabled={cancel.isPending || !!job.cancel_requested_at} onClick={() => cancel.mutate(job.id)} className="text-red-600 disabled:opacity-50">
        {job.cancel_requested_at ? 'Regrade cancellation requested' : 'Cancel regrade'}
      </button>}
    </div>)}
    <div className="flex gap-3 text-xs">
      {offset > 0 && <button onClick={() => setOffset(value => Math.max(0, value - 100))}>Previous regrade requests</button>}
      {jobs.data?.length === 100 && <button onClick={() => setOffset(value => value + 100)}>Next regrade requests</button>}
    </div>
  </section>
}
