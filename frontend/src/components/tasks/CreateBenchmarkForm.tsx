import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { benchmarkSdkApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { BenchmarkPreflight } from '@/types/benchmarks'
import { BenchmarkSelectionFields, type BenchmarkSelection } from './BenchmarkSelectionFields'

export default function CreateBenchmarkForm({ onClose }: { onClose: () => void }) {
  const queries = useQueryClient()
  const role = useAuth(state => state.workspaces.find(row => row.id === state.workspaceId)?.role)
  const writable = ['owner', 'admin', 'member'].includes(role ?? '')
  const [selection, setSelection] = useState<BenchmarkSelection>({ profile_id: '', dataset_revision_id: '', case_id: '', harness_profile_id: '' })
  const [preflight, setPreflight] = useState<BenchmarkPreflight>()
  const [title, setTitle] = useState('')
  const requests = useRef<Record<string, string>>({})
  const launch = useMutation({ mutationFn: () => {
    const document = { ...selection, ...(title.trim() ? { title: title.trim() } : {}) }
    const key = JSON.stringify(document)
    const request_id = requests.current[key] ??= crypto.randomUUID()
    return benchmarkSdkApi.createTrial({ request_id, ...document })
  }, onSuccess: () => { queries.invalidateQueries({ queryKey: ['tasks'] }); onClose() } })
  const ready = preflight?.installed && preflight.configured && preflight.compatible
  return <fieldset disabled={launch.isPending} className="space-y-3">
    <label className="block text-sm">Run title (optional)
      <input aria-label="Benchmark run title" value={title} maxLength={500} onChange={event => setTitle(event.target.value)}
        className="w-full border rounded px-2 py-1.5 text-sm" placeholder="Use the native case title" />
    </label>
    <BenchmarkSelectionFields value={selection} onChange={setSelection} onPreflight={setPreflight} />
    {launch.error && <p role="alert" className="text-sm text-red-600">{launch.error.message}</p>}
    <button type="button" disabled={!writable || !ready || launch.isPending} onClick={() => launch.mutate()}
      className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg disabled:opacity-50 text-sm font-medium">
      {launch.isPending ? 'Queueing…' : 'Queue benchmark run'}
    </button>
    <p className="text-xs text-gray-500">The run continues after this window closes. Its native environment and evaluation come from the selected benchmark.</p>
  </fieldset>
}
