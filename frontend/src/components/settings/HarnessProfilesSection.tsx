import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { harnessesApi, providersApi } from '@/api/client'
import type { HarnessManifest, HarnessProfile, HarnessSecret } from '@/types'
import { configurationErrors } from '@/lib/harnessSchema'
import { HarnessSettingsEditor } from './HarnessSettingsEditor'
import { ModelSelect } from './SystemModelsSection'

const inputClass = 'w-full border rounded px-2 py-1.5 text-sm'

export function HarnessCapabilities({ manifest }: { manifest?: HarnessManifest }) {
  if (!manifest) return <p className="text-sm text-amber-700">This connector version is unavailable.</p>
  const names = new Set(['feedback', 'abort', 'approval', ...Object.keys(manifest.capabilities)])
  return <div className="flex flex-wrap gap-2 text-xs" aria-label="Harness capabilities">
    {[...names].map(name => <span key={name} className="border rounded px-2 py-1 text-gray-600">
      {name}: {manifest.capabilities[name] ?? 'unsupported'}
    </span>)}
  </div>
}

function ProfileEditor({ profile, manifests, secrets, onClose }: {
  profile?: HarnessProfile; manifests: HarnessManifest[]; secrets: HarnessSecret[]; onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [connectorId, setConnectorId] = useState(profile?.connector_id ?? manifests[0]?.id ?? '')
  const [name, setName] = useState(profile?.name ?? '')
  const [config, setConfig] = useState<Record<string, unknown>>(profile?.config ?? {})
  const [modelId, setModelId] = useState<string | null>(profile?.model_id ?? null)
  const [isDefault, setIsDefault] = useState(profile?.is_default ?? false)
  const [refs, setRefs] = useState<Record<string, string>>(profile?.secret_refs ?? {})
  const [refName, setRefName] = useState('')
  const [refId, setRefId] = useState('')
  const [draftValid, setDraftValid] = useState(true)
  const validity = useCallback((valid: boolean) => setDraftValid(valid), [])
  const manifest = manifests.find(row => row.id === connectorId && (!profile || row.version === profile.connector_version))
  const errors = manifest ? configurationErrors(manifest.config_schema, config) : ['Pinned connector version is unavailable.']
  const { data: providers = [] } = useQuery({ queryKey: ['providers'], queryFn: providersApi.list })
  const save = useMutation({
    mutationFn: () => {
      const data = { name: name.trim(), connector_version: manifest!.version, config, secret_refs: refs, model_id: modelId, is_default: isDefault }
      return profile ? harnessesApi.update(profile.id, data) : harnessesApi.create({ ...data, connector_id: connectorId })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['harness-profiles'] })
      onClose()
    },
  })
  return <div className="border rounded-lg p-4 mt-3 space-y-4">
    <h3 className="font-medium">{profile ? 'Edit harness profile' : 'New harness profile'}</h3>
    <label className="block text-sm">Name
      <input aria-label="Profile name" value={name} onChange={event => setName(event.target.value)} className={inputClass} />
    </label>
    <label className="block text-sm">Installed connector
      <select aria-label="Installed connector" disabled={!!profile} className={inputClass} value={connectorId}
        onChange={event => { setConnectorId(event.target.value); setConfig({}); setDraftValid(true) }}>
        {manifests.map(row => <option key={row.id} value={row.id}>{row.display_name} · {row.version}</option>)}
        {!manifest && <option value={connectorId}>{connectorId} · unavailable</option>}
      </select>
    </label>
    <HarnessCapabilities manifest={manifest} />
    <div><label className="text-sm">Model connection</label>
      <ModelSelect providers={providers} value={modelId} onChange={setModelId} placeholder="— Select before execution —" />
      <p className="text-xs text-gray-500 mt-1">Credentials remain with the model provider. A run requires an available connection.</p>
    </div>
    {manifest && <HarnessSettingsEditor key={manifest.id} schema={manifest.config_schema} value={config}
      onChange={setConfig} onValidity={validity} />}
    <div className="space-y-2">
      <p className="text-sm font-medium">Credential references</p>
      {Object.entries(refs).map(([key, id]) => <div key={key} className="flex items-center gap-2 text-sm">
        <span className="flex-1">{key}: {secrets.find(secret => secret.id === id)?.name ?? 'Unavailable credential'}</span>
        <button type="button" className="text-red-600" onClick={() => setRefs(previous => {
          const next = { ...previous }; delete next[key]; return next
        })}>Remove</button>
      </div>)}
      <div className="flex gap-2">
        <input aria-label="Credential role" placeholder="Connector credential role" className={inputClass}
          value={refName} onChange={event => setRefName(event.target.value)} />
        <select aria-label="Stored credential" className={inputClass} value={refId} onChange={event => setRefId(event.target.value)}>
          <option value="">Select credential</option>
          {secrets.map(secret => <option key={secret.id} value={secret.id}>{secret.name}</option>)}
        </select>
        <button type="button" disabled={!refName.trim() || !refId} onClick={() => {
          setRefs(previous => ({ ...previous, [refName.trim()]: refId })); setRefName(''); setRefId('')
        }} className="text-sm text-blue-600 disabled:opacity-50">Add</button>
      </div>
    </div>
    <label className="flex gap-2 text-sm"><input type="checkbox" checked={isDefault} disabled={profile?.is_default}
      onChange={event => setIsDefault(event.target.checked)} />Workspace default</label>
    {errors.map((error, index) => <p key={index} role="alert" className="text-sm text-red-600">{error}</p>)}
    {save.error && <p role="alert" className="text-sm text-red-600">{save.error.message}</p>}
    <div className="flex gap-3">
      <button type="button" disabled={!name.trim() || !manifest || errors.length > 0 || !draftValid || save.isPending}
        className="bg-blue-600 text-white text-sm rounded px-3 py-2 disabled:opacity-50" onClick={() => save.mutate()}>Save profile</button>
      <button type="button" className="text-sm" onClick={onClose}>Cancel</button>
    </div>
  </div>
}

export function HarnessProfilesSection({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient()
  const catalog = useQuery({ queryKey: ['harnesses'], queryFn: harnessesApi.list })
  const profiles = useQuery({ queryKey: ['harness-profiles'], queryFn: harnessesApi.profiles })
  const secrets = useQuery({ queryKey: ['harness-secrets'], queryFn: harnessesApi.secrets })
  const [editing, setEditing] = useState<HarnessProfile | 'new' | null>(null)
  const [secretId, setSecretId] = useState('')
  const [secretName, setSecretName] = useState('')
  const [secretValue, setSecretValue] = useState('')
  const [notice, setNotice] = useState('')
  const action = useMutation({
    mutationFn: async ({ id, operation }: { id: string; operation: 'delete' | 'validate' | 'deleteSecret' }) => {
      if (operation === 'deleteSecret') await harnessesApi.deleteSecret(id)
      else if (operation === 'delete') await harnessesApi.delete(id)
      else await harnessesApi.validate(id)
      return operation
    },
    onSuccess: operation => {
      queryClient.invalidateQueries({ queryKey: ['harness-profiles'] })
      queryClient.invalidateQueries({ queryKey: ['harness-secrets'] })
      setNotice(operation === 'validate' ? 'Profile settings are valid.' : 'Removed.')
    },
  })
  const storeSecret = useMutation({
    mutationFn: () => secretId ? harnessesApi.updateSecret(secretId, secretName.trim(), secretValue)
      : harnessesApi.createSecret(secretName.trim(), secretValue),
    onSuccess: () => {
      setSecretValue(''); setSecretName(''); setSecretId('')
      queryClient.invalidateQueries({ queryKey: ['harness-secrets'] })
      setNotice('Credential stored. Its value cannot be read back.')
    },
  })
  const error = catalog.error ?? profiles.error ?? secrets.error ?? action.error ?? storeSecret.error
  return <section className="bg-white rounded-lg border p-4 mb-4">
    <div className="flex items-center justify-between mb-2">
      <h2 className="font-semibold">Harness profiles</h2>
      {canEdit && <button type="button" className="text-sm text-blue-600" disabled={!catalog.data?.length}
        onClick={() => setEditing('new')}>New profile</button>}
    </div>
    <p className="text-xs text-gray-500 mb-3">Choose an installed harness and its native settings. Connectors are installed on the server.</p>
    {(catalog.isPending || profiles.isPending) && <p className="text-sm">Loading harnesses…</p>}
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    {notice && <p role="status" className="text-sm text-green-700 mb-2">{notice}</p>}
    <div className="divide-y">
      {profiles.data?.map(profile => <div key={profile.id} className="py-3 space-y-2">
        <div className="flex items-center gap-2 text-sm">
          <span className="font-medium flex-1">{profile.name}{profile.is_default ? ' · default' : ''}</span>
          <span className="text-gray-500">{profile.connector_id} · {profile.connector_version}</span>
          {canEdit && <>
            <button type="button" className="text-blue-600" onClick={() => setEditing(profile)}>Edit</button>
            <button type="button" className="text-blue-600" disabled={action.isPending}
              onClick={() => action.mutate({ id: profile.id, operation: 'validate' })}>Validate</button>
            {!profile.is_default && <button type="button" className="text-red-600" disabled={action.isPending}
              onClick={() => action.mutate({ id: profile.id, operation: 'delete' })}>Delete</button>}
          </>}
        </div>
        <HarnessCapabilities manifest={catalog.data?.find(row => row.id === profile.connector_id && row.version === profile.connector_version)} />
      </div>)}
    </div>
    {canEdit && editing && <ProfileEditor key={editing === 'new' ? 'new' : editing.id}
      profile={editing === 'new' ? undefined : editing} manifests={catalog.data ?? []} secrets={secrets.data ?? []}
      onClose={() => setEditing(null)} />}
    <details className="mt-4 border-t pt-3">
      <summary className="text-sm font-medium cursor-pointer">Stored harness credentials</summary>
      <p className="text-xs text-gray-500 mt-2">Values are write-only. Model API keys are managed in Providers.</p>
      {secrets.data?.map(secret => <div key={secret.id} className="flex gap-2 text-sm py-2">
        <span className="flex-1">{secret.name} · configured</span>
        {canEdit && <>
          <button type="button" className="text-blue-600" onClick={() => {
            setSecretId(secret.id); setSecretName(secret.name); setSecretValue('')
          }}>Replace</button>
          <button type="button" className="text-red-600" disabled={action.isPending}
            onClick={() => action.mutate({ id: secret.id, operation: 'deleteSecret' })}>Delete</button>
        </>}
      </div>)}
      {canEdit && <div className="space-y-2 mt-3">
        <input aria-label="Credential name" autoComplete="off" placeholder="Credential name" className={inputClass}
          value={secretName} onChange={event => setSecretName(event.target.value)} />
        <input aria-label="New credential value" type="password" autoComplete="new-password" placeholder="New value" className={inputClass}
          value={secretValue} onChange={event => setSecretValue(event.target.value)} />
        <button type="button" disabled={!secretName.trim() || !secretValue || storeSecret.isPending}
          className="text-sm text-blue-600 disabled:opacity-50" onClick={() => storeSecret.mutate()}>
          {secretId ? 'Replace credential' : 'Store credential'}</button>
        {secretId && <button type="button" className="text-sm ml-3" onClick={() => {
          setSecretId(''); setSecretName(''); setSecretValue('')
        }}>Cancel replacement</button>}
      </div>}
    </details>
  </section>
}
