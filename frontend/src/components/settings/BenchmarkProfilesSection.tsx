import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { benchmarkSdkApi, providersApi } from '@/api/client'
import type { BenchmarkAvailability, BenchmarkCredential, BenchmarkManifest, BenchmarkProfile } from '@/types/benchmarks'
import { availabilityLabels, benchmarkModelRoles } from '@/lib/benchmarkStatus'
import { configurationErrors } from '@/lib/harnessSchema'
import { HarnessSettingsEditor } from './HarnessSettingsEditor'
import { ModelSelect } from './SystemModelsSection'

const inputClass = 'w-full border rounded px-2 py-1.5 text-sm'
const connectorKey = (manifest: BenchmarkManifest) => `${manifest.id}@${manifest.version}`

export function BenchmarkAvailabilityBadges({ value }: { value: BenchmarkAvailability }) {
  return <div className="flex flex-wrap gap-2 text-xs" aria-label="Benchmark availability">
    {availabilityLabels(value).map(label => <span key={label} className="border rounded px-2 py-1 text-gray-600">{label}</span>)}
  </div>
}

function ProfileEditor({ profile, manifests, credentials, onClose }: {
  profile?: BenchmarkProfile; manifests: BenchmarkManifest[]; credentials: BenchmarkCredential[]; onClose: () => void
}) {
  const qc = useQueryClient()
  const [selected, setSelected] = useState(profile ? `${profile.connector_id}@${profile.connector_version}` : manifests[0] ? connectorKey(manifests[0]) : '')
  const [name, setName] = useState(profile?.name ?? '')
  const [config, setConfig] = useState<Record<string, unknown>>(profile?.config ?? {})
  const [refs, setRefs] = useState<Record<string, string>>(profile?.secret_refs ?? {})
  const [models, setModels] = useState<Record<string, string>>(profile?.model_bindings ?? {})
  const [modelRole, setModelRole] = useState('')
  const [valid, setValid] = useState(true)
  const validity = useCallback((next: boolean) => setValid(next), [])
  const manifest = manifests.find(row => connectorKey(row) === selected)
  const errors = manifest ? configurationErrors(manifest.config_schema, config) : ['Pinned connector version is unavailable.']
  const providers = useQuery({ queryKey: ['providers'], queryFn: providersApi.list })
  const credentialNames = [...new Set([...Object.keys(manifest?.secret_schema?.properties ?? {}), ...Object.keys(refs)])]
  const save = useMutation({
    mutationFn: () => {
      const data = { name: name.trim(), connector_version: manifest!.version, config, secret_refs: refs,
        model_bindings: Object.fromEntries(Object.entries(models).filter(([, value]) => value)) }
      return profile ? benchmarkSdkApi.updateProfile(profile.id, data)
        : benchmarkSdkApi.createProfile({ ...data, connector_id: manifest!.id })
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['benchmark-profiles'] }); onClose() },
  })
  return <div className="border rounded-lg p-4 mt-3 space-y-4">
    <h3 className="font-medium">{profile ? 'Edit benchmark profile' : 'New benchmark profile'}</h3>
    <label className="block text-sm">Name
      <input aria-label="Benchmark profile name" className={inputClass} value={name} onChange={event => setName(event.target.value)} />
    </label>
    <label className="block text-sm">Installed benchmark connector
      <select aria-label="Benchmark connector" className={inputClass} value={selected} disabled={!!profile}
        onChange={event => { setSelected(event.target.value); setConfig({}); setRefs({}); setModels({}); setValid(true) }}>
        {manifests.map(row => <option key={connectorKey(row)} value={connectorKey(row)}>{row.display_name} · {row.version}</option>)}
        {!manifest && <option value={selected}>{selected || 'No connectors installed'} · unavailable</option>}
      </select>
    </label>
    {manifest && <>
      <div className="flex flex-wrap gap-2 text-xs" aria-label="Benchmark capabilities">
        {Object.entries(manifest.capabilities).map(([key, value]) => <span key={key} className="border rounded px-2 py-1">{key}: {value}</span>)}
      </div>
      <HarnessSettingsEditor key={selected} schema={manifest.config_schema} value={config} onChange={setConfig} onValidity={validity} />
    </>}
    {credentialNames.map(role => <label key={role} className="block text-sm">Credential · {role}{manifest?.secret_schema?.required?.includes(role) ? ' *' : ''}
      <select aria-label={`Benchmark credential ${role}`} className={inputClass} value={refs[role] ?? ''}
        onChange={event => setRefs(previous => {
          const next = { ...previous }; if (event.target.value) next[role] = event.target.value; else delete next[role]; return next
        })}>
        <option value="">Not configured</option>
        {credentials.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
        {refs[role] && !credentials.some(row => row.id === refs[role]) && <option value={refs[role]}>Unavailable credential</option>}
      </select>
    </label>)}
    {benchmarkModelRoles(manifest, models).map(role => <div key={role}>
      <label className="text-sm">Benchmark model · {role}</label>
      <ModelSelect providers={providers.data ?? []} value={models[role] ?? null} onChange={value => setModels(previous => {
        const next = { ...previous }; if (value) next[role] = value; else delete next[role]; return next
      })} />
    </div>)}
    <div className="flex gap-2">
      <input aria-label="Additional benchmark model role" placeholder="Additional model role (for example, user)" className={inputClass}
        value={modelRole} onChange={event => setModelRole(event.target.value)} />
      <button type="button" className="text-sm text-blue-600 disabled:opacity-50" disabled={!/^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/.test(modelRole)}
        onClick={() => { setModels(previous => ({ ...previous, [modelRole]: '' })); setModelRole('') }}>Add role</button>
    </div>
    <p className="text-xs text-gray-500">Model connections and credential references stay in this workspace. Validation checks settings; execution also requires a compatible case, harness and available resources.</p>
    {errors.map((error, index) => <p role="alert" className="text-sm text-red-600" key={index}>{error}</p>)}
    {(save.error ?? providers.error) && <p role="alert" className="text-sm text-red-600">{(save.error ?? providers.error)?.message}</p>}
    <div className="flex gap-3">
      <button type="button" className="bg-blue-600 text-white text-sm rounded px-3 py-2 disabled:opacity-50"
        disabled={!name.trim() || !manifest || !valid || errors.length > 0 || save.isPending} onClick={() => save.mutate()}>Save benchmark profile</button>
      <button type="button" className="text-sm" onClick={onClose}>Cancel</button>
    </div>
  </div>
}

export function BenchmarkProfilesSection({ canEdit }: { canEdit: boolean }) {
  const qc = useQueryClient()
  const catalog = useQuery({ queryKey: ['benchmark-catalog'], queryFn: benchmarkSdkApi.catalog })
  const profiles = useQuery({ queryKey: ['benchmark-profiles'], queryFn: benchmarkSdkApi.profiles })
  const credentials = useQuery({ queryKey: ['benchmark-credentials'], queryFn: benchmarkSdkApi.credentials })
  const [editing, setEditing] = useState<BenchmarkProfile | 'new' | null>(null)
  const [credentialId, setCredentialId] = useState('')
  const [credentialName, setCredentialName] = useState('')
  const [credentialValue, setCredentialValue] = useState('')
  const [notice, setNotice] = useState('')
  const resetCredential = () => { setCredentialId(''); setCredentialName(''); setCredentialValue('') }
  const action = useMutation({
    mutationFn: async ({ id, operation }: { id: string; operation: 'validate' | 'delete' | 'deleteCredential' }) => {
      if (operation === 'validate') {
        const result = await benchmarkSdkApi.validateProfile(id)
        return result.configured ? 'Settings are valid. Runtime verification is still required.' : `Settings are valid. Missing: ${result.missing.join(', ')}`
      }
      if (operation === 'delete') await benchmarkSdkApi.deleteProfile(id)
      else await benchmarkSdkApi.deleteCredential(id)
      return 'Removed.'
    },
    onSuccess: message => {
      qc.invalidateQueries({ queryKey: ['benchmark-profiles'] }); qc.invalidateQueries({ queryKey: ['benchmark-credentials'] }); setNotice(message)
    },
  })
  const storeCredential = useMutation({
    mutationFn: () => credentialId ? benchmarkSdkApi.replaceCredential(credentialId, credentialName.trim(), credentialValue)
      : benchmarkSdkApi.createCredential(credentialName.trim(), credentialValue),
    onSuccess: () => {
      resetCredential(); qc.invalidateQueries({ queryKey: ['benchmark-credentials'] }); qc.invalidateQueries({ queryKey: ['benchmark-profiles'] })
      setNotice('Credential stored. Its value cannot be read back.')
    },
  })
  const error = catalog.error ?? profiles.error ?? credentials.error ?? action.error ?? storeCredential.error
  return <section className="bg-white rounded-lg border p-4 mb-4">
    <div className="flex justify-between items-center mb-2">
      <h2 className="font-semibold">Benchmark profiles</h2>
      {canEdit && <button type="button" className="text-sm text-blue-600" disabled={!catalog.data?.length} onClick={() => setEditing('new')}>New benchmark profile</button>}
    </div>
    <p className="text-xs text-gray-500 mb-3">Configure installed benchmark connectors. Connector packages and dataset files are installed by the server administrator.</p>
    {(catalog.isPending || profiles.isPending) && <p className="text-sm">Loading benchmarks…</p>}
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    {notice && <p role="status" className="text-sm text-green-700 mb-2">{notice}</p>}
    {catalog.data?.length === 0 && <p className="text-sm text-gray-500">No benchmark connectors are installed.</p>}
    <div className="divide-y">
      {profiles.data?.map(profile => <div key={profile.id} className="py-3 space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium flex-1">{profile.name}</span>
          <span className="text-gray-500">{profile.connector_id} · {profile.connector_version}</span>
          {canEdit && <>
            <button type="button" className="text-blue-600" onClick={() => setEditing(profile)}>Edit</button>
            <button type="button" className="text-blue-600" disabled={action.isPending} onClick={() => action.mutate({ id: profile.id, operation: 'validate' })}>Validate</button>
            <button type="button" className="text-red-600" disabled={action.isPending} onClick={() => action.mutate({ id: profile.id, operation: 'delete' })}>Delete</button>
          </>}
        </div>
        <BenchmarkAvailabilityBadges value={profile} />
        {!!profile.missing?.length && <p className="text-xs text-amber-700">Missing: {profile.missing.join(', ')}</p>}
      </div>)}
    </div>
    {canEdit && editing && <ProfileEditor key={editing === 'new' ? 'new' : editing.id} profile={editing === 'new' ? undefined : editing}
      manifests={catalog.data?.map(row => row.manifest) ?? []} credentials={credentials.data ?? []} onClose={() => setEditing(null)} />}
    <details className="border-t pt-3 mt-4">
      <summary className="text-sm font-medium cursor-pointer">Stored benchmark credentials</summary>
      <p className="text-xs text-gray-500 mt-2">Values are write-only. Model API keys are managed in Providers.</p>
      {credentials.data?.map(row => <div key={row.id} className="flex gap-2 py-2 text-sm">
        <span className="flex-1">{row.name} · {row.configured ? 'configured' : 'unavailable'}</span>
        {canEdit && <>
          <button type="button" className="text-blue-600" onClick={() => { setCredentialId(row.id); setCredentialName(row.name); setCredentialValue('') }}>Replace</button>
          <button type="button" className="text-red-600" disabled={action.isPending} onClick={() => action.mutate({ id: row.id, operation: 'deleteCredential' })}>Delete</button>
        </>}
      </div>)}
      {canEdit && <div className="space-y-2 mt-3">
        <input className={inputClass} aria-label="Benchmark credential name" autoComplete="off" placeholder="Credential name" value={credentialName} onChange={event => setCredentialName(event.target.value)} />
        <input className={inputClass} aria-label="New benchmark credential value" type="password" autoComplete="new-password" placeholder="New value" value={credentialValue} onChange={event => setCredentialValue(event.target.value)} />
        <button type="button" className="text-sm text-blue-600 disabled:opacity-50" disabled={!credentialName.trim() || !credentialValue || storeCredential.isPending} onClick={() => storeCredential.mutate()}>
          {credentialId ? 'Replace benchmark credential' : 'Store benchmark credential'}</button>
        {credentialId && <button type="button" className="text-sm ml-3" onClick={resetCredential}>Cancel replacement</button>}
      </div>}
    </details>
  </section>
}
