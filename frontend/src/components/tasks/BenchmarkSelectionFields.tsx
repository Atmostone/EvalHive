import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { benchmarkSdkApi, harnessesApi } from '@/api/client'
import { BenchmarkAvailabilityBadges } from '@/components/settings/BenchmarkProfilesSection'
import type { BenchmarkPreflight } from '@/types/benchmarks'
import { preflightReason } from '@/lib/benchmarkStatus'

export interface BenchmarkSelection {
  profile_id: string
  dataset_revision_id: string
  case_id: string
  harness_profile_id: string
}

const inputClass = 'w-full border rounded px-2 py-1.5 text-sm'

export function BenchmarkSelectionFields({ value, onChange, onPreflight }: {
  value: BenchmarkSelection; onChange: (selection: BenchmarkSelection) => void
  onPreflight?: (result: BenchmarkPreflight | undefined) => void
}) {
  const [offset, setOffset] = useState(0)
  const profiles = useQuery({ queryKey: ['benchmark-profiles'], queryFn: benchmarkSdkApi.profiles })
  const datasets = useQuery({ queryKey: ['benchmark-datasets'], queryFn: benchmarkSdkApi.datasets })
  const harnesses = useQuery({ queryKey: ['harness-profiles'], queryFn: harnessesApi.profiles })
  const profile = profiles.data?.find(row => row.id === value.profile_id)
  const revisions = datasets.data?.filter(row => row.connector_id === profile?.connector_id && row.connector_version === profile?.connector_version) ?? []
  const dataset = revisions.find(row => row.id === value.dataset_revision_id)
  const cases = useQuery({ queryKey: ['benchmark-cases', value.dataset_revision_id, offset],
    queryFn: () => benchmarkSdkApi.cases(value.dataset_revision_id, offset), enabled: !!dataset })
  const complete = !!(profile && dataset && value.case_id && value.harness_profile_id)
  const preflight = useQuery({ queryKey: ['benchmark-preflight', value], queryFn: async () => {
    const result = await benchmarkSdkApi.preflight(value)
    return result
  }, enabled: complete, staleTime: 0, retry: false })
  useEffect(() => { onPreflight?.(complete && !preflight.isFetching && !preflight.error ? preflight.data : undefined) },
    [complete, preflight.data, preflight.isFetching, preflight.error, onPreflight])
  const change = (next: BenchmarkSelection) => { onPreflight?.(undefined); onChange(next) }
  const error = profiles.error ?? datasets.error ?? harnesses.error ?? cases.error ?? preflight.error
  return <div className="space-y-3">
    <label className="block text-sm">Benchmark profile
      <select aria-label="Run benchmark profile" className={inputClass} value={value.profile_id}
        onChange={event => { setOffset(0); change({ ...value, profile_id: event.target.value, dataset_revision_id: '', case_id: '' }) }}>
        <option value="">Select benchmark profile</option>
        {profiles.data?.map(row => <option key={row.id} value={row.id} disabled={!row.installed}>{row.name} · {row.connector_id}</option>)}
      </select>
    </label>
    {profile && <BenchmarkAvailabilityBadges value={profile} />}
    <label className="block text-sm">Dataset revision
      <select aria-label="Run dataset revision" className={inputClass} value={value.dataset_revision_id} disabled={!profile}
        onChange={event => { setOffset(0); change({ ...value, dataset_revision_id: event.target.value, case_id: '' }) }}>
        <option value="">Select registered dataset revision</option>
        {revisions.map(row => <option key={row.id} value={row.id}>{row.dataset_id} · {row.revision} · {row.case_count} cases</option>)}
      </select>
    </label>
    <label className="block text-sm">Case
      <select aria-label="Run benchmark case" className={inputClass} value={value.case_id} disabled={!dataset || cases.isFetching}
        onChange={event => change({ ...value, case_id: event.target.value })}>
        <option value="">Select case</option>
        {cases.data?.cases.map(row => <option key={row.id} value={row.id}>{row.title} · {row.ref.case_id}</option>)}
      </select>
    </label>
    {dataset && dataset.case_count > 100 && <div className="flex gap-3 text-xs items-center">
      <button type="button" disabled={offset === 0 || cases.isFetching} className="text-blue-600 disabled:opacity-50"
        onClick={() => { setOffset(Math.max(0, offset - 100)); change({ ...value, case_id: '' }) }}>Previous cases</button>
      <span>{offset + 1}–{Math.min(offset + (cases.data?.cases.length ?? 0), dataset.case_count)} of {dataset.case_count}</span>
      <button type="button" disabled={offset + 100 >= dataset.case_count || cases.isFetching} className="text-blue-600 disabled:opacity-50"
        onClick={() => { setOffset(offset + 100); change({ ...value, case_id: '' }) }}>Next cases</button>
    </div>}
    <label className="block text-sm">Harness profile
      <select aria-label="Benchmark harness profile" className={inputClass} value={value.harness_profile_id}
        onChange={event => change({ ...value, harness_profile_id: event.target.value })}>
        <option value="">Select harness profile</option>
        {harnesses.data?.map(row => <option key={row.id} value={row.id}>{row.name} · {row.connector_id}</option>)}
      </select>
    </label>
    {complete && preflight.isFetching && <p role="status" className="text-sm">Checking compatibility…</p>}
    {complete && preflight.data && <div className="space-y-2" aria-label="Benchmark preflight result">
      <BenchmarkAvailabilityBadges value={preflight.data} />
      {!!preflight.data.missing?.length && <p className="text-sm text-amber-700">Missing configuration: {preflight.data.missing.join(', ')}</p>}
      {preflight.data.reasons.map((reason, index) => <p key={index} className="text-sm text-amber-700">{preflightReason(reason)}</p>)}
      <p className="text-xs text-gray-500">Compatibility checks settings and declared requirements. Resources are reserved when execution starts.</p>
    </div>}
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
  </div>
}

export function BenchmarkPreflightSection() {
  const [selection, setSelection] = useState<BenchmarkSelection>({ profile_id: '', dataset_revision_id: '', case_id: '', harness_profile_id: '' })
  return <section className="bg-white rounded-lg border p-4 mb-4">
    <h2 className="font-semibold mb-3">Benchmark compatibility</h2>
    <BenchmarkSelectionFields value={selection} onChange={setSelection} />
  </section>
}
