import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { providersApi, templatesApi, rubricsApi, registryApi } from '@/api/client'
import type { Template } from '@/types'
import { Plus, Edit2, Trash2, Copy, X, Boxes, ServerIcon, AlertTriangle } from 'lucide-react'
import { ModelSelect } from '@/components/settings/SystemModelsSection'

function TemplateForm({ template, onClose }: { template?: Template; onClose: () => void }) {
  const queryClient = useQueryClient()
  const isEdit = !!template

  const [form, setForm] = useState({
    name: template?.name || '',
    description: template?.description || '',
    soul_md: template?.soul_md || '',
    model_id: template?.model_id ?? null as string | null,
    rubric_id: template?.rubric_id ?? null as string | null,
    tool_ids: (template?.tool_ids || []) as string[],
    max_ram: template?.max_ram || '2g',
    max_cpu: template?.max_cpu || 100000,
    timeout_minutes: template?.timeout_minutes || 60,
    tags: template?.tags || [],
  })

  const [tagsInput, setTagsInput] = useState(form.tags.join(', '))

  const { data: providers = [] } = useQuery({
    queryKey: ['providers'],
    queryFn: providersApi.list,
  })

  const { data: rubrics = [] } = useQuery({
    queryKey: ['rubrics'],
    queryFn: rubricsApi.list,
  })

  const { data: registry = [] } = useQuery({
    queryKey: ['registry', 'tools'],
    queryFn: () => registryApi.list(),
  })

  const createMutation = useMutation({
    mutationFn: () => templatesApi.create(form as Parameters<typeof templatesApi.create>[0]),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['templates'] }); onClose() },
  })

  const updateMutation = useMutation({
    mutationFn: () => templatesApi.update(template!.id, form),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['templates'] }); onClose() },
  })

  function handleSubmit() {
    const data = {
      ...form,
      tags: tagsInput.split(',').map(s => s.trim()).filter(Boolean),
    }
    Object.assign(form, data)
    if (isEdit) {
      updateMutation.mutate()
    } else {
      createMutation.mutate()
    }
  }

  const set = (field: string, value: unknown) => setForm(prev => ({ ...prev, [field]: value }))

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto p-6 shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">{isEdit ? 'Edit Template' : 'New Template'}</h2>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-100"><X className="h-5 w-5" /></button>
        </div>

        <div className="space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
            <input value={form.name} onChange={e => set('name', e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="e.g. Coder" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Description (for orchestrator)</label>
            <textarea value={form.description} onChange={e => set('description', e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm h-16 resize-none" placeholder="When to use this template..." />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">SOUL.md (agent personality)</label>
            <textarea value={form.soul_md} onChange={e => set('soul_md', e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm h-32 resize-y font-mono" placeholder="You are an expert..." />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Model</label>
            <ModelSelect
              providers={providers}
              value={form.model_id}
              onChange={(v) => set('model_id', v)}
              placeholder="— Pick a model —"
            />
            {!form.model_id && (
              <p className="text-xs text-orange-600 mt-1">
                Without a model, the agent cannot be spawned.
              </p>
            )}
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Max RAM</label>
              <input value={form.max_ram} onChange={e => set('max_ram', e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Max CPU (%)</label>
              <input type="number" value={form.max_cpu / 1000} onChange={e => set('max_cpu', Number(e.target.value) * 1000)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Timeout (min)</label>
              <input type="number" value={form.timeout_minutes} onChange={e => set('timeout_minutes', Number(e.target.value))}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Tags (comma-separated)</label>
              <input value={tagsInput} onChange={e => setTagsInput(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="coding, python" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Quality rubric</label>
              <select value={form.rubric_id ?? ''}
                onChange={e => set('rubric_id', e.target.value || null)}
                className="w-full px-3 py-2 border rounded-lg text-sm">
                <option value="">— Auto (by tag / default) —</option>
                {rubrics.map(r => (
                  <option key={r.id} value={r.id}>{r.name}</option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2 flex items-center gap-1">
              <ServerIcon className="h-4 w-4" /> Tools &amp; MCP
              <span className="text-xs font-normal text-gray-400">(registry)</span>
            </label>
            {registry.length === 0 ? (
              <p className="text-xs text-gray-400">
                No registry entries yet — add tools &amp; MCP servers in Settings → Tool &amp; MCP Registry.
              </p>
            ) : (
              <div className="border rounded-lg divide-y max-h-56 overflow-y-auto">
                {registry.map(e => {
                  const checked = form.tool_ids.includes(e.id)
                  return (
                    <label key={e.id}
                      className="flex items-center gap-2 px-3 py-2 text-sm hover:bg-gray-50 cursor-pointer">
                      <input type="checkbox" checked={checked}
                        onChange={() => set('tool_ids', checked
                          ? form.tool_ids.filter(i => i !== e.id)
                          : [...form.tool_ids, e.id])} />
                      <span className="font-medium text-gray-800">{e.name}</span>
                      <span className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border ${
                        e.kind === 'mcp'
                          ? 'bg-purple-50 text-purple-700 border-purple-200'
                          : 'bg-gray-50 text-gray-500 border-gray-200'}`}>
                        {e.kind}
                      </span>
                      {!e.enabled && <span className="text-[10px] text-amber-600">disabled</span>}
                    </label>
                  )
                })}
              </div>
            )}
            <p className="text-xs text-gray-400 mt-1">
              Select from the workspace registry — configure entries in Settings.
            </p>
          </div>

          <button onClick={handleSubmit}
            disabled={!form.name.trim() || !form.soul_md.trim()}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium">
            {isEdit ? 'Save Changes' : 'Create Template'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Templates() {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<Template | undefined>()
  const [showCreate, setShowCreate] = useState(false)

  const { data: templates = [] } = useQuery({
    queryKey: ['templates'],
    queryFn: () => templatesApi.list(),
  })

  const { data: registry = [] } = useQuery({
    queryKey: ['registry', 'tools'],
    queryFn: () => registryApi.list(),
  })
  const registryName = (id: string) => registry.find(e => e.id === id)?.name ?? id.slice(0, 8)

  const deleteMutation = useMutation({
    mutationFn: (id: string) => templatesApi.delete(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['templates'] }),
  })

  const duplicateMutation = useMutation({
    mutationFn: (t: Template) => templatesApi.create({
      name: `${t.name} (copy)`,
      description: t.description,
      soul_md: t.soul_md,
      model_id: t.model_id,
      model_display_name: null,
      model_api_name: null,
      provider_name: null,
      rubric_id: t.rubric_id,
      tool_ids: t.tool_ids,
      max_ram: t.max_ram,
      max_cpu: t.max_cpu,
      timeout_minutes: t.timeout_minutes,
      tags: t.tags,
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['templates'] }),
  })

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Templates</h1>
        <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
          <Plus className="h-4 w-4" /> New Template
        </button>
      </div>

      {templates.length === 0 ? (
        <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
          <Boxes className="h-12 w-12 mx-auto mb-3 text-gray-300" />
          <p>No templates yet</p>
          <p className="text-sm mt-1">Create a template to define agent roles</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {templates.map(t => (
            <div key={t.id} className="bg-white rounded-lg border p-4 hover:shadow-md transition-shadow">
              <div className="flex items-start justify-between mb-2">
                <h3 className="font-semibold text-gray-900">{t.name}</h3>
                <div className="flex gap-1">
                  <button onClick={() => setEditing(t)} className="p-1 rounded hover:bg-gray-100" title="Edit">
                    <Edit2 className="h-4 w-4 text-gray-400" />
                  </button>
                  <button onClick={() => duplicateMutation.mutate(t)} className="p-1 rounded hover:bg-gray-100" title="Duplicate">
                    <Copy className="h-4 w-4 text-gray-400" />
                  </button>
                  <button onClick={() => { if (confirm('Delete this template?')) deleteMutation.mutate(t.id) }}
                    className="p-1 rounded hover:bg-red-50" title="Delete">
                    <Trash2 className="h-4 w-4 text-gray-400 hover:text-red-500" />
                  </button>
                </div>
              </div>
              <p className="text-sm text-gray-500 mb-3 line-clamp-2">{t.description}</p>
              <div className="flex flex-wrap gap-1.5">
                {t.model_id ? (
                  <span className="text-xs px-2 py-0.5 rounded-full bg-purple-100 text-purple-700">
                    {t.provider_name ? `${t.provider_name} / ` : ''}{t.model_display_name || t.model_api_name}
                  </span>
                ) : (
                  <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-orange-100 text-orange-700">
                    <AlertTriangle className="h-3 w-3" /> Not configured
                  </span>
                )}
                {(t.tool_ids || []).map(id => (
                  <span key={id} className="text-xs px-2 py-0.5 rounded-full bg-blue-100 text-blue-700">{registryName(id)}</span>
                ))}
              </div>
              <div className="mt-2 text-xs text-gray-400">
                RAM: {t.max_ram} | CPU: {t.max_cpu / 1000}% | Timeout: {t.timeout_minutes}m
              </div>
            </div>
          ))}
        </div>
      )}

      {(showCreate || editing) && (
        <TemplateForm
          template={editing}
          onClose={() => { setShowCreate(false); setEditing(undefined) }}
        />
      )}
    </div>
  )
}
