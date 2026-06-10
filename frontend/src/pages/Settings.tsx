import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { settingsApi, healthApi, agentsApi, knowledgeApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import { Save, RefreshCw, Skull, Download, Trash } from 'lucide-react'
import { ProvidersSection } from '@/components/settings/ProvidersSection'
import { SystemModelsSection } from '@/components/settings/SystemModelsSection'

const ORCHESTRATOR_FIELDS = [
  { key: 'max_concurrent_agents', label: 'Max Concurrent Agents', type: 'number' },
  { key: 'task_timeout_minutes', label: 'Task Timeout (min)', type: 'number' },
  { key: 'max_retries', label: 'Max Retries', type: 'number' },
]

// Bias Mitigation Toolkit (E-18). Verbosity & score-clustering append an
// instruction to the judge prompt; self-preference flags judge==agent; position
// is reserved for pairwise judging (E-21).
const BIAS_TOGGLES = [
  { key: 'bias_mitigation_verbosity', label: 'Verbosity', desc: 'Instruct the judge to ignore answer length and judge substance.' },
  { key: 'bias_mitigation_score_clustering', label: 'Score clustering', desc: 'Instruct the judge to use the full 0–10 range instead of defaulting to 7–8.' },
  { key: 'bias_mitigation_self_preference', label: 'Self-preference', desc: 'Flag when the judge model is the same as the agent model (scores may be inflated).' },
  { key: 'bias_mitigation_position', label: 'Position', desc: 'Pairwise order-swap — no-op until pairwise judging (E-21) exists.' },
]

const STORAGE_FIELDS = [
  { key: 'minio_endpoint', label: 'MinIO Endpoint', envVar: 'MINIO_ENDPOINT' },
  { key: 'minio_access_key', label: 'MinIO Access Key', envVar: 'MINIO_ROOT_USER' },
  { key: 'minio_secret_key', label: 'MinIO Secret Key', envVar: 'MINIO_ROOT_PASSWORD' },
]

export default function Settings() {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<Record<string, unknown>>({})
  const [saved, setSaved] = useState(false)

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
  })

  const { data: health, refetch: refetchHealth } = useQuery({
    queryKey: ['health'],
    queryFn: healthApi.check,
  })

  const role = useAuth((s) => s.workspaces.find((w) => w.id === s.workspaceId)?.role ?? null)
  const isAdminRole = role === 'owner' || role === 'admin'

  useEffect(() => {
    if (settings) setForm(settings)
  }, [settings])

  const saveMutation = useMutation({
    mutationFn: () => settingsApi.update(form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    },
  })

  const killAllMutation = useMutation({
    mutationFn: () => agentsApi.killAll(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['agents'] }),
  })

  const resetRagMutation = useMutation({
    mutationFn: () => knowledgeApi.reset(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['knowledge', 'documents'] }),
  })

  const set = (key: string, value: unknown) => {
    setForm(prev => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  const provider = (form.embedding_provider as string) || 'fastembed'

  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Settings</h1>

      {/* Health */}
      <div className="bg-white rounded-lg border p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">System Health</h2>
          <button onClick={() => refetchHealth()} className="p-1 rounded hover:bg-gray-100">
            <RefreshCw className="h-4 w-4 text-gray-400" />
          </button>
        </div>
        {health && (
          <div className="flex gap-3">
            {Object.entries(health.services).map(([name, status]) => (
              <div key={name} className="flex items-center gap-2 text-sm">
                <div className={`w-2 h-2 rounded-full ${status === 'ok' ? 'bg-green-500' : 'bg-red-500'}`} />
                <span className="capitalize">{name}</span>
              </div>
            ))}
            <span className="text-xs text-gray-400 ml-auto">v{health.version}</span>
          </div>
        )}
      </div>

      {/* Providers & Models */}
      <ProvidersSection canEdit={isAdminRole} />

      {/* System Models */}
      <SystemModelsSection canEdit={isAdminRole} />

      {/* Orchestrator */}
      <div className="bg-white rounded-lg border p-4 mb-4">
        <h2 className="font-semibold mb-3">Orchestrator</h2>
        <div className="space-y-3">
          {ORCHESTRATOR_FIELDS.map(({ key, label, type }) => (
            <div key={key}>
              <label className="block text-sm font-medium text-gray-700 mb-1">{label}</label>
              <input
                type={type}
                value={form[key] != null ? String(form[key]) : ''}
                onChange={e => {
                  const val = type === 'number' ? Number(e.target.value) : e.target.value
                  set(key, val)
                }}
                className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          ))}
          <div className="flex items-start gap-2 pt-1">
            <input
              id="decomposition_enabled"
              type="checkbox"
              checked={form.decomposition_enabled !== false}
              onChange={e => set('decomposition_enabled', e.target.checked)}
              className="mt-1"
            />
            <label htmlFor="decomposition_enabled" className="text-sm text-gray-700">
              <span className="font-medium">Enable task decomposition</span>
              <div className="text-xs text-gray-500">
                When off, every root task is handled by a single agent (no auto-split into subtasks).
              </div>
            </label>
          </div>
        </div>
      </div>

      {/* Bias Mitigation (E-18) */}
      <div className="bg-white rounded-lg border p-4 mb-4">
        <h2 className="font-semibold mb-1">
          Bias Mitigation <span className="text-sm font-normal text-gray-400">(E-18)</span>
        </h2>
        <p className="text-xs text-gray-500 mb-3">
          Counter-measures for known LLM-judge biases. Run the A/B report on the Analytics page
          to measure their effect against human ratings.
        </p>
        <div className="space-y-3">
          {BIAS_TOGGLES.map(({ key, label, desc }) => (
            <div key={key} className="flex items-start gap-2">
              <input
                id={key}
                type="checkbox"
                checked={form[key] === true}
                onChange={e => set(key, e.target.checked)}
                className="mt-1"
              />
              <label htmlFor={key} className="text-sm text-gray-700">
                <span className="font-medium">{label}</span>
                <div className="text-xs text-gray-500">{desc}</div>
              </label>
            </div>
          ))}
        </div>
      </div>

      {/* Embedding Model */}
      <div className="bg-white rounded-lg border p-4 mb-4">
        <h2 className="font-semibold mb-3">Embedding Model</h2>
        <div className="space-y-3">
          <div className="flex gap-4 text-sm">
            <label className="flex items-center gap-2">
              <input type="radio" name="embedding_provider" value="fastembed"
                checked={provider === 'fastembed'}
                onChange={() => set('embedding_provider', 'fastembed')} />
              fastembed (local CPU)
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="embedding_provider" value="api"
                checked={provider === 'api'}
                onChange={() => set('embedding_provider', 'api')} />
              External API
            </label>
          </div>
          {provider === 'fastembed' ? (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Local model</label>
              <input
                value={(form.embedding_model_local as string) || ''}
                onChange={e => set('embedding_model_local', e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm"
              />
            </div>
          ) : (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">API URL</label>
                <input
                  value={(form.embedding_api_url as string) || ''}
                  onChange={e => set('embedding_api_url', e.target.value)}
                  className="w-full px-3 py-2 border rounded-lg text-sm"
                  placeholder="https://.../v1/embeddings"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">API Key</label>
                <input
                  type="password"
                  value={(form.embedding_api_key as string) || ''}
                  onChange={e => set('embedding_api_key', e.target.value)}
                  className="w-full px-3 py-2 border rounded-lg text-sm"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Model name</label>
                <input
                  value={(form.embedding_model_api as string) || ''}
                  onChange={e => set('embedding_model_api', e.target.value)}
                  className="w-full px-3 py-2 border rounded-lg text-sm"
                />
              </div>
            </>
          )}
          <div className="flex items-center gap-3 pt-2 border-t">
            <button
              onClick={() => {
                if (confirm('Reset RAG? This drops the Qdrant collection and deletes ALL uploaded documents.')) {
                  resetRagMutation.mutate()
                }
              }}
              disabled={resetRagMutation.isPending}
              className="flex items-center gap-2 px-3 py-1.5 border border-orange-200 text-orange-700 rounded-lg hover:bg-orange-50 disabled:opacity-50 text-sm"
            >
              <Trash className="h-4 w-4" /> {resetRagMutation.isPending ? 'Resetting...' : 'Reset RAG'}
            </button>
            <span className="text-xs text-gray-500">
              Required when switching provider — vector dim may change.
            </span>
          </div>
        </div>
      </div>

      {/* Storage (read-only) */}
      <div className="bg-white rounded-lg border p-4 mb-4">
        <h2 className="font-semibold mb-1">Storage (MinIO)</h2>
        <p className="text-xs text-gray-500 mb-3">Configured via .env, restart required to change.</p>
        <div className="space-y-3">
          {STORAGE_FIELDS.map(({ key, label, envVar }) => (
            <div key={key}>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                {label} <span className="text-xs text-gray-400">({envVar})</span>
              </label>
              <input
                disabled
                value={form[key] != null ? String(form[key]) : ''}
                className="w-full px-3 py-2 border rounded-lg text-sm bg-gray-50 text-gray-500"
              />
            </div>
          ))}
        </div>
      </div>

      <button
        onClick={() => saveMutation.mutate()}
        disabled={saveMutation.isPending}
        className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
      >
        <Save className="h-4 w-4" /> {saved ? 'Saved!' : 'Save Settings'}
      </button>

      {/* System — admin/owner only */}
      {isAdminRole && (
        <div className="bg-white rounded-lg border p-4 mt-6 space-y-3">
          <h2 className="font-semibold">System</h2>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => {
                if (confirm('Kill ALL running agent containers?')) killAllMutation.mutate()
              }}
              disabled={killAllMutation.isPending}
              className="flex items-center gap-2 px-3 py-1.5 border border-red-200 text-red-600 rounded-lg hover:bg-red-50 disabled:opacity-50 text-sm font-medium"
            >
              <Skull className="h-4 w-4" />
              {killAllMutation.isPending
                ? 'Killing...'
                : killAllMutation.data
                  ? `Killed ${killAllMutation.data.killed}`
                  : 'Kill All Agents'}
            </button>
            <a
              href="/api/settings/export-all"
              className="flex items-center gap-2 px-3 py-1.5 border border-gray-200 text-gray-700 rounded-lg hover:bg-gray-50 text-sm font-medium"
            >
              <Download className="h-4 w-4" /> Export All Data
            </a>
          </div>
        </div>
      )}
    </div>
  )
}
