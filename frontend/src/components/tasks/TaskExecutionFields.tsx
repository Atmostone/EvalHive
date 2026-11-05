import { useQuery } from '@tanstack/react-query'
import { harnessesApi, templatesApi } from '@/api/client'
import type { Task } from '@/types'
import { HarnessCapabilities } from '../settings/HarnessProfilesSection'

type Selection = Pick<Task, 'execution_mode' | 'harness_profile_id' | 'template_id'>

export default function TaskExecutionFields({ value, onChange, disabled = false }: {
  value: Selection; onChange: (selection: Partial<Selection>) => void; disabled?: boolean
}) {
  const profiles = useQuery({ queryKey: ['harness-profiles'], queryFn: harnessesApi.profiles })
  const catalog = useQuery({ queryKey: ['harnesses'], queryFn: harnessesApi.list })
  const templates = useQuery({ queryKey: ['templates'], queryFn: templatesApi.list })
  const role = templates.data?.find(row => row.id === value.template_id)
  const mode = value.execution_mode ?? role?.execution_mode ?? 'orchestrated'
  const profileId = value.harness_profile_id ?? role?.harness_profile_id
  const profile = profiles.data?.find(row => profileId ? row.id === profileId : row.is_default)
  const manifest = catalog.data?.find(row => row.id === profile?.connector_id && row.version === profile?.connector_version)
  const error = profiles.error ?? catalog.error ?? templates.error
  const inputClass = 'w-full border rounded px-2 py-1.5 text-sm mt-1'
  return <div className="space-y-3 border rounded-lg p-3">
    <label className="block text-sm font-medium">Execution mode
      <select aria-label="Task execution mode" disabled={disabled} className={inputClass} value={value.execution_mode ?? ''}
        onChange={event => onChange({ execution_mode: event.target.value as Task['execution_mode'] || null })}>
        <option value="">Use role default</option>
        <option value="direct">Direct</option><option value="orchestrated">Orchestrated</option>
      </select>
    </label>
    <label className="block text-sm font-medium">Role (optional)
      <select aria-label="Task role" disabled={disabled} className={inputClass} value={value.template_id ?? ''}
        onChange={event => onChange({ template_id: event.target.value || null })}>
        <option value="">{mode === 'direct' ? 'No role' : 'Automatic selection'}</option>
        {templates.data?.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
      </select>
    </label>
    <label className="block text-sm font-medium">Harness profile
      <select aria-label="Task harness profile" disabled={disabled} className={inputClass} value={value.harness_profile_id ?? ''}
        onChange={event => onChange({ harness_profile_id: event.target.value || null })}>
        <option value="">Role / workspace default</option>
        {profiles.data?.map(row => <option key={row.id} value={row.id}>{row.name} · {row.connector_id}</option>)}
      </select>
    </label>
    <p className="text-xs text-gray-500">{mode === 'direct'
      ? 'Runs the selected harness directly. No orchestrator or automatic evaluation is called.'
      : 'Uses the orchestrator workflow and its configured system model.'}</p>
    {profile && <><p className="text-xs text-gray-600">Applied profile: {profile.name}</p><HarnessCapabilities manifest={manifest} /></>}
    {mode === 'direct' && manifest?.requirements?.model_api && profile && !profile.model_id && !role?.model_id && <p className="text-xs text-amber-700">
      Choose a model connection in this profile or role before starting.
    </p>}
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
  </div>
}
