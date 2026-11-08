import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { benchmarkSdkApi } from '@/api/client'
import { useAuth } from '@/stores/auth'

export default function BenchmarkAggregates({ trialId, active }: { trialId: string; active: boolean }) {
  const [offset, setOffset] = useState(0)
  const [nativeInputs, setNativeInputs] = useState(false)
  const role = useAuth(state => state.workspaces.find(row => row.id === state.workspaceId)?.role)
  const exportArchive = useMutation({ mutationFn: async () => {
    const blob = await benchmarkSdkApi.archive(trialId, nativeInputs)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a'); link.href = url; link.download = `native-trial-${trialId}.json`; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } })
  const records = useQuery({ queryKey: ['benchmark-aggregates', trialId, offset],
    queryFn: () => benchmarkSdkApi.aggregates(trialId, offset), refetchInterval: active ? 2000 : false })
  return <section aria-label="Native aggregation history" className="space-y-2 text-xs">
    <h4 className="font-medium text-sm">Native aggregation history</h4>
    <p className="text-gray-500">Each result covers its recorded population and benchmark version. Separate configurations and repetitions are not combined.</p>
    {records.error && <p role="alert" className="text-red-600">{records.error.message}</p>}
    {records.data?.map(record => <div className="border rounded p-2 space-y-1" key={record.id}>
      <p>Native aggregate · {record.status}{record.origin?.config_key ? ` · ${record.origin.config_key} · repeat ${(record.origin.run_index ?? 0) + 1}` : ''}</p>
      <p>Coverage: {record.observed} / {record.expected ?? 'unknown'} · {record.complete ? 'complete population' : 'incomplete population'}</p>
      {record.reason && <p className="text-amber-700">{record.reason}</p>}
      {record.metrics.map(metric => <p key={metric.name}>{metric.name}: {metric.value == null ? 'unknown' : String(metric.value)}</p>)}
      {record.population_digest && <details><summary className="cursor-pointer">Recorded population identity</summary><p className="break-all">{record.population_digest}</p></details>}
    </div>)}
    {!records.data?.length && <p className="text-gray-500">No native aggregate receipt recorded.</p>}
    {!active && <div className="space-y-2">
      {['owner', 'admin'].includes(role ?? '') && <label className="block"><input type="checkbox" checked={nativeInputs} onChange={event => setNativeInputs(event.target.checked)} /> Include private checker outputs for offline aggregation</label>}
      <button disabled={exportArchive.isPending} onClick={() => exportArchive.mutate()} className="text-blue-600 disabled:opacity-50">Export native record</button>
      {exportArchive.error && <p role="alert" className="text-red-600">{exportArchive.error.message}</p>}
    </div>}
    <div className="flex gap-3">
      {offset > 0 && <button onClick={() => setOffset(value => Math.max(0, value - 20))}>Previous aggregates</button>}
      {records.data?.length === 20 && <button onClick={() => setOffset(value => value + 20)}>Next aggregates</button>}
    </div>
  </section>
}
