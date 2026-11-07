import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { experimentsApi, harnessesApi, type ExperimentCreateBody } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { BenchmarkPreflight } from '@/types/benchmarks'
import { BenchmarkSelectionFields, type BenchmarkSelection } from '@/components/tasks/BenchmarkSelectionFields'

export default function NativeBenchmarkExperimentForm({ onClose }: { onClose: () => void }) {
  const queries = useQueryClient()
  const navigate = useNavigate()
  const role = useAuth(state => state.workspaces.find(row => row.id === state.workspaceId)?.role)
  const [name, setName] = useState('')
  const [repeats, setRepeats] = useState(1)
  const [selection, setSelection] = useState<BenchmarkSelection>({ profile_id: '', dataset_revision_id: '', case_id: '', harness_profile_id: '' })
  const [preflight, setPreflight] = useState<BenchmarkPreflight>()
  const [additional, setAdditional] = useState<string[]>([])
  const profiles = useQuery({ queryKey: ['harness-profiles'], queryFn: harnessesApi.profiles })
  const participants = [...new Set([selection.harness_profile_id, ...additional].filter(Boolean))]
  const body: ExperimentCreateBody = {
    name: name.trim(), dataset: { source: 'benchmark_sdk', profile_id: selection.profile_id, dataset_revision_id: selection.dataset_revision_id, case_ids: [selection.case_id] },
    configurations: participants.map(harness_profile_id => ({ harness_profile_id })), n_runs_per_cell: repeats, max_parallel: 1,
  }
  const bodyKey = JSON.stringify(body)
  const [previewKey, setPreviewKey] = useState('')
  const preview = useMutation({ mutationFn: () => experimentsApi.preview(body), onSuccess: () => setPreviewKey(bodyKey) })
  const create = useMutation({ mutationFn: () => experimentsApi.create(body), onSuccess: result => {
    queries.invalidateQueries({ queryKey: ['experiments'] }); onClose(); navigate(`/experiments/${result.id}`)
  } })
  const ready = ['owner', 'admin'].includes(role ?? '') && !!name.trim() && preflight?.installed && preflight.configured && preflight.compatible
    && Number.isInteger(repeats) && repeats >= 1 && repeats <= 20 && participants.length <= 24
  return <fieldset disabled={create.isPending || preview.isPending} className="space-y-4">
    <label className="block text-sm">Name
      <input aria-label="Native matrix name" value={name} maxLength={255} onChange={event => setName(event.target.value)} className="block w-full border rounded px-2 py-1.5 mt-1" />
    </label>
    <BenchmarkSelectionFields value={selection} onChange={setSelection} onPreflight={setPreflight} />
    <label className="block text-sm">Additional harness profiles to compare
      <select aria-label="Additional matrix harness profiles" multiple value={additional}
        onChange={event => setAdditional(Array.from(event.target.selectedOptions, option => option.value))} className="block w-full border rounded p-2 mt-1">
        {profiles.data?.filter(row => row.id !== selection.harness_profile_id).map(row => <option key={row.id} value={row.id}>{row.name} · {row.connector_id}</option>)}
      </select>
    </label>
    <label className="block text-sm">Repetitions per case and configuration
      <input aria-label="Native matrix repetitions" type="number" min={1} max={20} value={repeats} onChange={event => setRepeats(Number(event.target.value))} className="block w-full border rounded px-2 py-1.5 mt-1" />
    </label>
    <p className="text-sm">{participants.length} configurations × 1 case × {Number.isInteger(repeats) ? repeats : '—'} repetitions</p>
    <p className="text-xs text-gray-500">Settings are frozen when the draft is created. Each run uses the benchmark's native environment and evaluation. Runs start after you start the draft and execute one at a time.</p>
    <button type="button" onClick={() => preview.mutate()} disabled={!ready} className="text-sm text-blue-600 disabled:opacity-50">Check entire native matrix</button>
    {preview.data && previewKey === bodyKey && <div className="text-sm bg-blue-50 p-3 rounded" role="status">
      {preview.data.total_runs} runs checked. Cost and duration are unknown until recorded.
    </div>}
    {(create.error ?? preview.error) && <p role="alert" className="text-sm text-red-600">{(create.error ?? preview.error)?.message}</p>}
    <button type="button" onClick={() => create.mutate()} disabled={!ready} className="w-full px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50 text-sm">
      {create.isPending ? 'Freezing native matrix…' : 'Create native matrix draft'}
    </button>
  </fieldset>
}
