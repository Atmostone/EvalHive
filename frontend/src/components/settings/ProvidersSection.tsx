import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { providersApi, modelsApi } from '@/api/client'
import type { LLMModel, Provider, ModelTestResponse } from '@/types'
import { Plug, Plus, Save, Trash, X, ChevronDown, ChevronRight } from 'lucide-react'

interface ProviderFormState {
  name: string
  endpoint: string
  api_key: string
  max_concurrency: string
}

// '' → undefined (no limit set), digits → number; used on create/update bodies.
function parseConcurrency(raw: string): number | undefined {
  const n = parseInt(raw, 10)
  return Number.isFinite(n) && n > 0 ? n : undefined
}

interface ModelFormState {
  display_name: string
  api_name: string
  input_price_per_1m_usd: string
  output_price_per_1m_usd: string
}

const EMPTY_PROVIDER: ProviderFormState = { name: '', endpoint: '', api_key: '', max_concurrency: '' }
const EMPTY_MODEL: ModelFormState = {
  display_name: '',
  api_name: '',
  input_price_per_1m_usd: '0',
  output_price_per_1m_usd: '0',
}

export function ProvidersSection({ canEdit }: { canEdit: boolean }) {
  const qc = useQueryClient()
  const [addingProvider, setAddingProvider] = useState(false)
  const [providerDraft, setProviderDraft] = useState<ProviderFormState>(EMPTY_PROVIDER)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const { data: providers } = useQuery({
    queryKey: ['providers'],
    queryFn: providersApi.list,
  })

  const createProvider = useMutation({
    mutationFn: () =>
      providersApi.create({
        name: providerDraft.name,
        endpoint: providerDraft.endpoint,
        api_key: providerDraft.api_key,
        max_concurrency: parseConcurrency(providerDraft.max_concurrency),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers'] })
      setProviderDraft(EMPTY_PROVIDER)
      setAddingProvider(false)
    },
  })

  return (
    <div className="bg-white rounded-lg border p-4 mb-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="font-semibold">Providers & Models</h2>
          <p className="text-xs text-gray-500">
            LLM providers (OpenAI/OpenRouter/local) and models offered by each. Per-1M-token prices feed into per-task cost.
          </p>
        </div>
        {canEdit && !addingProvider && (
          <button
            onClick={() => setAddingProvider(true)}
            className="flex items-center gap-1 px-2 py-1 border rounded text-sm hover:bg-gray-50"
          >
            <Plus className="h-4 w-4" /> Add provider
          </button>
        )}
      </div>

      {addingProvider && (
        <div className="border border-blue-200 bg-blue-50 rounded p-3 mb-3 space-y-2">
          <div className="grid grid-cols-3 gap-2">
            <input
              placeholder="Name (e.g. OpenAI)"
              value={providerDraft.name}
              onChange={(e) => setProviderDraft({ ...providerDraft, name: e.target.value })}
              className="px-2 py-1 border rounded text-sm"
            />
            <input
              placeholder="Endpoint URL"
              value={providerDraft.endpoint}
              onChange={(e) => setProviderDraft({ ...providerDraft, endpoint: e.target.value })}
              className="px-2 py-1 border rounded text-sm col-span-2"
            />
          </div>
          <input
            type="password"
            placeholder="API key"
            value={providerDraft.api_key}
            onChange={(e) => setProviderDraft({ ...providerDraft, api_key: e.target.value })}
            className="w-full px-2 py-1 border rounded text-sm"
          />
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              placeholder="Max concurrency"
              value={providerDraft.max_concurrency}
              onChange={(e) => setProviderDraft({ ...providerDraft, max_concurrency: e.target.value })}
              className="w-40 px-2 py-1 border rounded text-sm"
            />
            <span className="text-xs text-gray-500">
              Max simultaneous LLM calls to this provider (empty = unlimited; subscription plans often cap concurrent requests)
            </span>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => createProvider.mutate()}
              disabled={
                createProvider.isPending ||
                !providerDraft.name ||
                !providerDraft.endpoint ||
                !providerDraft.api_key
              }
              className="flex items-center gap-1 px-3 py-1 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
            >
              <Save className="h-4 w-4" /> Save
            </button>
            <button
              onClick={() => {
                setProviderDraft(EMPTY_PROVIDER)
                setAddingProvider(false)
              }}
              className="flex items-center gap-1 px-3 py-1 border rounded text-sm hover:bg-gray-50"
            >
              <X className="h-4 w-4" /> Cancel
            </button>
          </div>
        </div>
      )}

      {(providers ?? []).length === 0 && !addingProvider && (
        <p className="text-sm text-gray-500 italic">
          No providers configured yet. Add your first provider to enable LLM-driven features.
        </p>
      )}

      <div className="space-y-2">
        {(providers ?? []).map((p) => (
          <ProviderCard
            key={p.id}
            provider={p}
            expanded={!!expanded[p.id]}
            onToggle={() => setExpanded((s) => ({ ...s, [p.id]: !s[p.id] }))}
            canEdit={canEdit}
          />
        ))}
      </div>
    </div>
  )
}

function ProviderCard({
  provider,
  expanded,
  onToggle,
  canEdit,
}: {
  provider: Provider
  expanded: boolean
  onToggle: () => void
  canEdit: boolean
}) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<ProviderFormState>({
    name: provider.name,
    endpoint: provider.endpoint,
    api_key: '',
    max_concurrency: provider.max_concurrency != null ? String(provider.max_concurrency) : '',
  })

  const update = useMutation({
    mutationFn: () => {
      const concurrencyTouched =
        draft.max_concurrency !== (provider.max_concurrency != null ? String(provider.max_concurrency) : '')
      const patch = {
        name: draft.name !== provider.name ? draft.name : undefined,
        endpoint: draft.endpoint !== provider.endpoint ? draft.endpoint : undefined,
        api_key: draft.api_key ? draft.api_key : undefined,
        // 0 tells the backend to clear the limit (empty input → unlimited)
        max_concurrency: concurrencyTouched ? (parseConcurrency(draft.max_concurrency) ?? 0) : undefined,
      }
      return providersApi.update(provider.id, patch)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers'] })
      setEditing(false)
      setDraft({ ...draft, api_key: '' })
    },
  })

  const remove = useMutation({
    mutationFn: () => providersApi.remove(provider.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['providers'] }),
  })

  return (
    <div className="border rounded">
      <div className="flex items-center justify-between p-2 hover:bg-gray-50">
        <button onClick={onToggle} className="flex items-center gap-1 text-sm flex-1 text-left">
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <span className="font-medium">{provider.name}</span>
          <span className="text-xs text-gray-500 ml-2 truncate max-w-[260px]">{provider.endpoint}</span>
          <span className="text-xs text-gray-400 ml-2 font-mono">{provider.api_key_masked}</span>
          {provider.max_concurrency != null && (
            <span className="text-xs text-amber-600 ml-2" title="Max simultaneous LLM calls">
              ≤{provider.max_concurrency} parallel
            </span>
          )}
        </button>
        {canEdit && (
          <div className="flex gap-1">
            <button
              onClick={() => setEditing((s) => !s)}
              className="px-2 py-0.5 text-xs border rounded hover:bg-gray-100"
            >
              {editing ? 'Cancel' : 'Edit'}
            </button>
            <button
              onClick={() => {
                if (confirm(`Delete provider "${provider.name}" and all its models?`)) {
                  remove.mutate()
                }
              }}
              className="px-2 py-0.5 text-xs border border-red-200 text-red-600 rounded hover:bg-red-50"
            >
              Delete
            </button>
          </div>
        )}
      </div>
      {editing && (
        <div className="border-t bg-gray-50 p-3 space-y-2">
          <div className="grid grid-cols-3 gap-2">
            <input
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              className="px-2 py-1 border rounded text-sm"
            />
            <input
              value={draft.endpoint}
              onChange={(e) => setDraft({ ...draft, endpoint: e.target.value })}
              className="px-2 py-1 border rounded text-sm col-span-2"
            />
          </div>
          <input
            type="password"
            placeholder="Leave empty to keep current API key"
            value={draft.api_key}
            onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
            className="w-full px-2 py-1 border rounded text-sm"
          />
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              placeholder="Max concurrency"
              value={draft.max_concurrency}
              onChange={(e) => setDraft({ ...draft, max_concurrency: e.target.value })}
              className="w-40 px-2 py-1 border rounded text-sm"
            />
            <span className="text-xs text-gray-500">empty = unlimited</span>
          </div>
          <button
            onClick={() => update.mutate()}
            disabled={update.isPending}
            className="flex items-center gap-1 px-3 py-1 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
          >
            <Save className="h-4 w-4" /> Save
          </button>
        </div>
      )}
      {expanded && <ModelsList providerId={provider.id} canEdit={canEdit} />}
    </div>
  )
}

function ModelsList({ providerId, canEdit }: { providerId: string; canEdit: boolean }) {
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<ModelFormState>(EMPTY_MODEL)

  const { data: models } = useQuery({
    queryKey: ['providers', providerId, 'models'],
    queryFn: () => providersApi.listModels(providerId),
  })

  const create = useMutation({
    mutationFn: () =>
      providersApi.createModel(providerId, {
        display_name: draft.display_name,
        api_name: draft.api_name,
        input_price_per_1m_usd: Number(draft.input_price_per_1m_usd) || 0,
        output_price_per_1m_usd: Number(draft.output_price_per_1m_usd) || 0,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers', providerId, 'models'] })
      // Templates dropdown depends on the full model list — refresh it too.
      qc.invalidateQueries({ queryKey: ['templates'] })
      setDraft(EMPTY_MODEL)
      setAdding(false)
    },
  })

  return (
    <div className="border-t p-3 space-y-2 bg-gray-50">
      <div className="flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wider text-gray-500">Models</h3>
        {canEdit && !adding && (
          <button
            onClick={() => setAdding(true)}
            className="flex items-center gap-1 px-2 py-0.5 border rounded text-xs hover:bg-white"
          >
            <Plus className="h-3 w-3" /> Add model
          </button>
        )}
      </div>

      {adding && (
        <div className="border border-blue-200 bg-white rounded p-2 space-y-1">
          <div className="grid grid-cols-2 gap-2">
            <input
              placeholder="Display name (your label)"
              value={draft.display_name}
              onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
              className="px-2 py-1 border rounded text-sm"
            />
            <input
              placeholder="API name (sent to the endpoint)"
              value={draft.api_name}
              onChange={(e) => setDraft({ ...draft, api_name: e.target.value })}
              className="px-2 py-1 border rounded text-sm"
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs text-gray-600">
              Input $ / 1M tok
              <input
                type="number"
                step="0.000001"
                value={draft.input_price_per_1m_usd}
                onChange={(e) => setDraft({ ...draft, input_price_per_1m_usd: e.target.value })}
                className="w-full px-2 py-1 border rounded text-sm mt-0.5"
              />
            </label>
            <label className="text-xs text-gray-600">
              Output $ / 1M tok
              <input
                type="number"
                step="0.000001"
                value={draft.output_price_per_1m_usd}
                onChange={(e) => setDraft({ ...draft, output_price_per_1m_usd: e.target.value })}
                className="w-full px-2 py-1 border rounded text-sm mt-0.5"
              />
            </label>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => create.mutate()}
              disabled={create.isPending || !draft.display_name || !draft.api_name}
              className="flex items-center gap-1 px-3 py-1 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
            >
              <Save className="h-4 w-4" /> Add
            </button>
            <button
              onClick={() => {
                setDraft(EMPTY_MODEL)
                setAdding(false)
              }}
              className="flex items-center gap-1 px-3 py-1 border rounded text-sm hover:bg-white"
            >
              <X className="h-4 w-4" /> Cancel
            </button>
          </div>
        </div>
      )}

      {(models ?? []).length === 0 && !adding && (
        <p className="text-xs text-gray-500 italic">No models for this provider.</p>
      )}

      <div className="space-y-1">
        {(models ?? []).map((m) => (
          <ModelRow key={m.id} model={m} providerId={providerId} canEdit={canEdit} />
        ))}
      </div>
    </div>
  )
}

function ModelRow({
  model,
  providerId,
  canEdit,
}: {
  model: LLMModel
  providerId: string
  canEdit: boolean
}) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [test, setTest] = useState<ModelTestResponse | null>(null)
  const [draft, setDraft] = useState<ModelFormState>({
    display_name: model.display_name,
    api_name: model.api_name,
    input_price_per_1m_usd: String(model.input_price_per_1m_usd),
    output_price_per_1m_usd: String(model.output_price_per_1m_usd),
  })

  const update = useMutation({
    mutationFn: () =>
      modelsApi.update(model.id, {
        display_name: draft.display_name,
        api_name: draft.api_name,
        input_price_per_1m_usd: Number(draft.input_price_per_1m_usd) || 0,
        output_price_per_1m_usd: Number(draft.output_price_per_1m_usd) || 0,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers', providerId, 'models'] })
      qc.invalidateQueries({ queryKey: ['templates'] })
      setEditing(false)
    },
  })

  const remove = useMutation({
    mutationFn: () => modelsApi.remove(model.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers', providerId, 'models'] })
      qc.invalidateQueries({ queryKey: ['templates'] })
      qc.invalidateQueries({ queryKey: ['workspaces', 'system-models'] })
    },
  })

  const testMutation = useMutation({
    mutationFn: () => modelsApi.test(model.id),
    onSuccess: setTest,
    onError: (e: Error) => setTest({ status: 'error', error: e.message }),
  })

  return (
    <div className="bg-white border rounded">
      <div className="flex items-center justify-between px-2 py-1.5">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium truncate">{model.display_name}</div>
          <div className="text-xs text-gray-500 truncate">
            {model.api_name} · in $
            {Number(model.input_price_per_1m_usd).toFixed(3)} / out $
            {Number(model.output_price_per_1m_usd).toFixed(3)} per 1M
          </div>
        </div>
        <div className="flex gap-1 items-center">
          {test && (
            <span
              className={`text-xs px-1.5 py-0.5 rounded ${
                test.status === 'ok' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
              }`}
            >
              {test.status === 'ok' ? `${test.latency_ms}ms` : (test.error || 'failed').slice(0, 40)}
            </span>
          )}
          <button
            onClick={() => {
              setTest(null)
              testMutation.mutate()
            }}
            disabled={testMutation.isPending}
            className="flex items-center gap-1 px-2 py-0.5 border rounded text-xs hover:bg-gray-50"
          >
            <Plug className="h-3 w-3" /> {testMutation.isPending ? '...' : 'Test'}
          </button>
          {canEdit && (
            <>
              <button
                onClick={() => setEditing((s) => !s)}
                className="px-2 py-0.5 border rounded text-xs hover:bg-gray-50"
              >
                {editing ? 'Cancel' : 'Edit'}
              </button>
              <button
                onClick={() => {
                  if (confirm(`Delete model "${model.display_name}"?`)) remove.mutate()
                }}
                className="p-1 border border-red-200 text-red-600 rounded hover:bg-red-50"
              >
                <Trash className="h-3 w-3" />
              </button>
            </>
          )}
        </div>
      </div>
      {editing && (
        <div className="border-t bg-gray-50 p-2 space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <input
              value={draft.display_name}
              onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
              className="px-2 py-1 border rounded text-sm"
            />
            <input
              value={draft.api_name}
              onChange={(e) => setDraft({ ...draft, api_name: e.target.value })}
              className="px-2 py-1 border rounded text-sm"
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs text-gray-600">
              Input $ / 1M
              <input
                type="number"
                step="0.000001"
                value={draft.input_price_per_1m_usd}
                onChange={(e) => setDraft({ ...draft, input_price_per_1m_usd: e.target.value })}
                className="w-full px-2 py-1 border rounded text-sm mt-0.5"
              />
            </label>
            <label className="text-xs text-gray-600">
              Output $ / 1M
              <input
                type="number"
                step="0.000001"
                value={draft.output_price_per_1m_usd}
                onChange={(e) => setDraft({ ...draft, output_price_per_1m_usd: e.target.value })}
                className="w-full px-2 py-1 border rounded text-sm mt-0.5"
              />
            </label>
          </div>
          <button
            onClick={() => update.mutate()}
            disabled={update.isPending}
            className="flex items-center gap-1 px-3 py-1 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
          >
            <Save className="h-4 w-4" /> Save
          </button>
        </div>
      )}
    </div>
  )
}
