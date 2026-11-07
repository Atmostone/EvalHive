import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { benchmarkSdkApi } from '@/api/client'
import type { BenchmarkDataset } from '@/types/benchmarks'

const inputClass = 'w-full border rounded px-2 py-1.5 text-sm'

function DatasetCases({ dataset }: { dataset: BenchmarkDataset }) {
  const [offset, setOffset] = useState(0)
  const page = useQuery({ queryKey: ['benchmark-cases', dataset.id, offset], queryFn: () => benchmarkSdkApi.cases(dataset.id, offset) })
  const total = page.data?.total ?? dataset.case_count
  return <div className="space-y-2 mt-2">
    {page.error && <p role="alert" className="text-sm text-red-600">{page.error.message}</p>}
    {page.isPending && <p className="text-sm">Loading cases…</p>}
    <ul className="max-h-72 overflow-y-auto text-xs divide-y">
      {page.data?.cases.map(row => <li key={row.id} className="py-2">
        <p className="font-medium">{row.title}</p>
        <p className="text-gray-500 break-all">{row.ref.case_id}</p>
        {!!row.description && <details><summary className="cursor-pointer">Task description</summary><p className="whitespace-pre-wrap py-1">{row.description}</p></details>}
      </li>)}
    </ul>
    <div className="flex items-center gap-3 text-xs">
      <button type="button" disabled={offset === 0 || page.isFetching} className="text-blue-600 disabled:opacity-50" onClick={() => setOffset(Math.max(0, offset - 100))}>Previous cases</button>
      <span>{total ? offset + 1 : 0}–{Math.min(offset + (page.data?.cases.length ?? 0), total)} of {total}</span>
      <button type="button" disabled={offset + 100 >= total || page.isFetching} className="text-blue-600 disabled:opacity-50" onClick={() => setOffset(offset + 100)}>Next cases</button>
    </div>
  </div>
}

export function BenchmarkDatasetsSection({ canEdit }: { canEdit: boolean }) {
  const qc = useQueryClient()
  const profiles = useQuery({ queryKey: ['benchmark-profiles'], queryFn: benchmarkSdkApi.profiles })
  const datasets = useQuery({ queryKey: ['benchmark-datasets'], queryFn: benchmarkSdkApi.datasets })
  const [profileId, setProfileId] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [revision, setRevision] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const register = useMutation({
    mutationFn: () => benchmarkSdkApi.registerDataset(profileId, datasetId.trim(), revision.trim()),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['benchmark-datasets'] }); setDatasetId(''); setRevision('') },
  })
  const error = profiles.error ?? datasets.error ?? register.error
  return <section className="bg-white rounded-lg border p-4 mb-4">
    <h2 className="font-semibold mb-2">Benchmark datasets</h2>
    <p className="text-xs text-gray-500 mb-3">Register an installed dataset revision in this workspace. Revisions preserve their source identity and case catalog.</p>
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    {datasets.isPending && <p className="text-sm">Loading dataset revisions…</p>}
    {datasets.data?.length === 0 && <p className="text-sm text-gray-500">No dataset revisions registered.</p>}
    <div className="divide-y">
      {datasets.data?.map(row => <div key={row.id} className="py-3 space-y-1 text-sm">
        <div className="flex gap-2 items-center"><span className="font-medium flex-1 break-all">{row.dataset_id} · {row.revision}</span>
          <button type="button" className="text-blue-600" aria-expanded={expanded === row.id} onClick={() => setExpanded(expanded === row.id ? null : row.id)}>Browse cases</button>
        </div>
        <p className="text-xs text-gray-500">{row.connector_id} · {row.connector_version} · {row.case_count} cases</p>
        <details className="text-xs text-gray-500"><summary className="cursor-pointer">Source identity</summary>
          <p className="break-all">Source: {row.source_digest}</p><p className="break-all">Receipt: {row.receipt_digest}</p><p className="break-all">Worker: {row.worker_image}</p>
        </details>
        {expanded === row.id && <DatasetCases key={row.id} dataset={row} />}
      </div>)}
    </div>
    {canEdit && <details className="border-t pt-3 mt-3">
      <summary className="text-sm font-medium cursor-pointer">Register installed dataset revision</summary>
      <div className="space-y-2 mt-3">
        <label className="block text-sm">Benchmark profile
          <select aria-label="Dataset benchmark profile" className={inputClass} value={profileId} onChange={event => setProfileId(event.target.value)}>
            <option value="">Select benchmark profile</option>
            {profiles.data?.map(row => <option key={row.id} value={row.id} disabled={!row.installed}>{row.name} · {row.connector_id}</option>)}
          </select>
        </label>
        <label className="block text-sm">Installed dataset ID
          <input aria-label="Installed dataset ID" className={inputClass} value={datasetId} onChange={event => setDatasetId(event.target.value)} />
        </label>
        <label className="block text-sm">Installed revision
          <input aria-label="Installed dataset revision" className={inputClass} value={revision} onChange={event => setRevision(event.target.value)} />
        </label>
        <p className="text-xs text-gray-500">Use the dataset ID and revision from the server installation receipt. Registering metadata does not download data or install executable code.</p>
        <button type="button" className="text-sm text-blue-600 disabled:opacity-50" disabled={!profileId || !datasetId.trim() || !revision.trim() || register.isPending}
          onClick={() => register.mutate()}>Register revision</button>
        {register.isSuccess && <p role="status" className="text-sm text-green-700">Dataset revision registered.</p>}
      </div>
    </details>}
  </section>
}
