import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { experimentsApi, providersApi, templatesApi } from '@/api/client'
import type { ExperimentCreateBody } from '@/api/client'
import type { Experiment, ExperimentStatus, LLMModel } from '@/types'
import { FlaskConical, Plus, X } from 'lucide-react'

export const STATUS_COLORS: Record<ExperimentStatus, string> = {
  draft: 'bg-gray-100 text-gray-600',
  running: 'bg-blue-100 text-blue-700',
  paused: 'bg-amber-100 text-amber-700',
  completed: 'bg-green-100 text-green-700',
  capped: 'bg-orange-100 text-orange-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-200 text-gray-500',
}

export function StatusPill({ status }: { status: ExperimentStatus }) {
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_COLORS[status] || 'bg-gray-100 text-gray-600'}`}>
      {status}
    </span>
  )
}

type DatasetSource = 'upload' | 'benchmark_suite' | 'tasks'

interface ConfigDraft {
  label: string
  orchestrator: boolean
  template_id: string
  model_id: string
  temperature: string
  seed: string
  soul_md: string
  memory_mode: string
}

function emptyConfig(): ConfigDraft {
  return { label: '', orchestrator: false, template_id: '', model_id: '', temperature: '', seed: '', soul_md: '', memory_mode: '' }
}

function configToPayload(c: ConfigDraft): Record<string, unknown> {
  const out: Record<string, unknown> = { orchestrator: c.orchestrator }
  if (c.label.trim()) out.label = c.label.trim()
  if (!c.orchestrator && c.template_id) out.template_id = c.template_id
  if (c.model_id) out.model_id = c.model_id
  if (c.temperature !== '') out.temperature = Number(c.temperature)
  if (c.seed !== '') out.seed = Number(c.seed)
  if (c.soul_md.trim()) out.soul_md = c.soul_md
  if (c.memory_mode) out.memory_mode = c.memory_mode
  return out
}

function parseJsonl(text: string): { cases: Record<string, unknown>[]; errors: string[] } {
  const cases: Record<string, unknown>[] = []
  const errors: string[] = []
  text.split('\n').forEach((line, i) => {
    const trimmed = line.trim()
    if (!trimmed) return
    try {
      const obj = JSON.parse(trimmed)
      if (typeof obj !== 'object' || obj === null || Array.isArray(obj)) {
        errors.push(`line ${i + 1}: expected a JSON object`)
      } else {
        cases.push(obj as Record<string, unknown>)
      }
    } catch {
      errors.push(`line ${i + 1}: invalid JSON`)
    }
  })
  return { cases, errors }
}

function ExperimentForm({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [source, setSource] = useState<DatasetSource>('upload')
  const [jsonl, setJsonl] = useState('')
  const [suite, setSuite] = useState('')
  const [taskIds, setTaskIds] = useState('')
  const [configs, setConfigs] = useState<ConfigDraft[]>([emptyConfig(), emptyConfig()])
  const [nRuns, setNRuns] = useState(3)
  const [budget, setBudget] = useState('')
  const [maxParallel, setMaxParallel] = useState('')
  const [submitError, setSubmitError] = useState('')

  const { data: templates = [] } = useQuery({ queryKey: ['templates'], queryFn: templatesApi.list })
  const { data: models = [] } = useQuery({
    queryKey: ['all-models'],
    queryFn: async () => {
      const providers = await providersApi.list()
      const lists = await Promise.all(providers.map((p) => providersApi.listModels(p.id)))
      return lists.flat() as LLMModel[]
    },
  })

  const { cases: uploadCases, errors: uploadErrors } = useMemo(() => parseJsonl(jsonl), [jsonl])

  const dataset = useMemo(() => {
    if (source === 'upload') return { source, cases: uploadCases }
    if (source === 'benchmark_suite') return { source, suite: suite.trim() }
    return {
      source,
      task_ids: taskIds.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean),
    }
  }, [source, uploadCases, suite, taskIds])

  const body: ExperimentCreateBody = useMemo(
    () => ({
      name: name.trim(),
      description: description.trim() || null,
      dataset,
      configurations: configs.map(configToPayload),
      n_runs_per_cell: nRuns,
      budget_limit_usd: budget !== '' ? Number(budget) : null,
      max_parallel: maxParallel !== '' ? Number(maxParallel) : null,
    }),
    [name, description, dataset, configs, nRuns, budget, maxParallel],
  )

  const datasetReady =
    (source === 'upload' && uploadCases.length > 0 && uploadErrors.length === 0) ||
    (source === 'benchmark_suite' && suite.trim() !== '') ||
    (source === 'tasks' && (dataset as { task_ids?: string[] }).task_ids!.length > 0)
  const configsReady = configs.every((c) => c.orchestrator || c.template_id)
  const ready = name.trim() !== '' && datasetReady && configsReady && nRuns >= 1

  const { data: preview, error: previewError } = useQuery({
    queryKey: ['exp-preview', JSON.stringify(body)],
    queryFn: () => experimentsApi.preview(body),
    enabled: ready,
    staleTime: 10_000,
    retry: false,
  })

  const createMutation = useMutation({
    mutationFn: () => experimentsApi.create(body),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
      onClose()
      navigate(`/experiments/${created.id}`)
    },
    onError: (e: Error) => setSubmitError(e.message),
  })

  const setConfig = (idx: number, patch: Partial<ConfigDraft>) =>
    setConfigs((prev) => prev.map((c, i) => (i === idx ? { ...c, ...patch } : c)))

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl w-full max-w-3xl max-h-[92vh] overflow-y-auto p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">New Experiment</h2>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-100"><X className="h-5 w-5" /></button>
        </div>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="MiniMax vs DeepSeek on Researcher" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
              <input value={description} onChange={(e) => setDescription(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="optional" />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Dataset</label>
            <div className="flex gap-2 mb-2">
              {(['upload', 'benchmark_suite', 'tasks'] as DatasetSource[]).map((s) => (
                <button key={s} type="button" onClick={() => setSource(s)}
                  className={`px-3 py-1.5 rounded-lg text-sm border ${source === s ? 'bg-blue-600 text-white border-blue-600' : 'hover:bg-gray-50'}`}>
                  {s === 'upload' ? 'Upload JSONL' : s === 'benchmark_suite' ? 'Benchmark suite' : 'Existing tasks'}
                </button>
              ))}
            </div>
            {source === 'upload' && (
              <div>
                <textarea value={jsonl} onChange={(e) => setJsonl(e.target.value)}
                  className="w-full px-3 py-2 border rounded-lg text-xs font-mono h-28"
                  placeholder={'{"task_input": {"title": "...", "description": "..."}, "reference_answer": "..."}\none JSON object per line'} />
                <div className="text-xs mt-1">
                  <span className="text-gray-500">{uploadCases.length} case(s) parsed</span>
                  {uploadErrors.map((err) => (
                    <div key={err} className="text-red-500">{err}</div>
                  ))}
                </div>
              </div>
            )}
            {source === 'benchmark_suite' && (
              <input value={suite} onChange={(e) => setSuite(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="suite name, e.g. capability-isolation" />
            )}
            {source === 'tasks' && (
              <textarea value={taskIds} onChange={(e) => setTaskIds(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-xs font-mono h-20"
                placeholder="task UUIDs, comma or newline separated" />
            )}
          </div>

          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium text-gray-700">Configurations (A/B matrix)</label>
              <button type="button" onClick={() => setConfigs((p) => [...p, emptyConfig()])}
                className="text-xs px-2 py-1 border rounded hover:bg-gray-50">+ Add configuration</button>
            </div>
            {configs.map((c, idx) => (
              <div key={idx} className="border rounded-lg p-3 mb-2 bg-gray-50 space-y-2">
                <div className="flex items-center justify-between">
                  <input placeholder={`label (e.g. baseline)`} value={c.label}
                    onChange={(e) => setConfig(idx, { label: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white w-64" />
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-1.5 text-xs text-gray-600" title="on: full orchestration (decompose + select); off: direct agent spawn with the pinned template">
                      <input type="checkbox" checked={c.orchestrator}
                        onChange={(e) => setConfig(idx, { orchestrator: e.target.checked, template_id: e.target.checked ? '' : c.template_id })} />
                      orchestrator
                    </label>
                    {configs.length > 1 && (
                      <button type="button" onClick={() => setConfigs((p) => p.filter((_, i) => i !== idx))}
                        className="text-xs text-red-500 hover:underline">remove</button>
                    )}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <select value={c.template_id} disabled={c.orchestrator}
                    onChange={(e) => setConfig(idx, { template_id: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white disabled:bg-gray-100 disabled:text-gray-400">
                    <option value="">{c.orchestrator ? 'template: selected by orchestrator' : 'template (required)'}</option>
                    {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                  </select>
                  <select value={c.model_id} onChange={(e) => setConfig(idx, { model_id: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white">
                    <option value="">model: template default</option>
                    {models.map((m) => <option key={m.id} value={m.id}>{m.display_name} ({m.api_name})</option>)}
                  </select>
                </div>
                <div className="grid grid-cols-3 gap-2">
                  <input type="number" step="0.1" min={0} max={2} placeholder="temperature" value={c.temperature}
                    onChange={(e) => setConfig(idx, { temperature: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white" />
                  <input type="number" placeholder="seed" value={c.seed}
                    onChange={(e) => setConfig(idx, { seed: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white" />
                  <select value={c.memory_mode} onChange={(e) => setConfig(idx, { memory_mode: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white">
                    <option value="">memory: workspace default</option>
                    <option value="off">off</option>
                    <option value="flat">flat</option>
                    <option value="structured">structured</option>
                  </select>
                </div>
                <textarea placeholder="soul_md override (system prompt) — leave empty to use the template's" value={c.soul_md}
                  onChange={(e) => setConfig(idx, { soul_md: e.target.value })}
                  className="w-full px-2 py-1.5 border rounded text-xs font-mono bg-white h-14" />
              </div>
            ))}
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Runs per cell (N)</label>
              <input type="number" min={1} max={20} value={nRuns}
                onChange={(e) => setNRuns(Number(e.target.value))}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Budget limit (USD)</label>
              <input type="number" step="0.01" min={0} value={budget} placeholder="no limit"
                onChange={(e) => setBudget(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Max parallel</label>
              <input type="number" min={1} max={10} value={maxParallel} placeholder="auto"
                onChange={(e) => setMaxParallel(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>

          {ready && preview && (
            <div className="border rounded-lg p-3 bg-blue-50 text-sm">
              <span className="font-medium">{preview.total_runs} runs</span>
              <span className="text-gray-600"> = {preview.n_configs} configs × {preview.n_cases} cases × {preview.n_runs_per_cell} · </span>
              <span className="text-gray-700">est. ${preview.est_cost_usd.toFixed(2)} · ~{preview.est_duration_minutes} min</span>
              {preview.warnings.map((w) => (
                <div key={w} className="text-amber-700 text-xs mt-1">⚠ {w}</div>
              ))}
            </div>
          )}
          {ready && previewError != null && (
            <div className="text-red-500 text-xs">{(previewError as Error).message}</div>
          )}
          {submitError && <div className="text-red-500 text-xs whitespace-pre-wrap">{submitError}</div>}

          <button onClick={() => { setSubmitError(''); createMutation.mutate() }}
            disabled={!ready || createMutation.isPending}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium">
            Create Experiment (draft)
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Experiments() {
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)

  const { data: experiments = [] } = useQuery({
    queryKey: ['experiments'],
    queryFn: () => experimentsApi.list(),
    refetchInterval: 5000,
  })

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-2xl font-bold text-gray-900">Experiments</h1>
        <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
          <Plus className="h-4 w-4" /> New Experiment
        </button>
      </div>
      <p className="text-sm text-gray-500 mb-6">
        A/B matrix harness: a frozen dataset × a matrix of agent configurations × N runs per cell,
        executed over the benchmark path (no orchestrator noise, no human in the loop) with evaluation
        always on. Compare models, prompts, toolsets and orchestration itself — with statistics.
      </p>

      {experiments.length === 0 ? (
        <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
          <FlaskConical className="h-12 w-12 mx-auto mb-3 text-gray-300" />
          <p>No experiments yet</p>
        </div>
      ) : (
        <div className="bg-white rounded-lg border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-4 py-2">Name</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Matrix</th>
                <th className="px-4 py-2">Cost</th>
                <th className="px-4 py-2">Created</th>
              </tr>
            </thead>
            <tbody>
              {experiments.map((e: Experiment) => (
                <tr key={e.id} onClick={() => navigate(`/experiments/${e.id}`)}
                  className="border-t hover:bg-gray-50 cursor-pointer">
                  <td className="px-4 py-2.5 font-medium text-gray-900">{e.name}</td>
                  <td className="px-4 py-2.5"><StatusPill status={e.status} /></td>
                  <td className="px-4 py-2.5 text-gray-600">
                    {e.n_configs} × {e.n_cases} × {e.n_runs_per_cell} = {e.total_runs} runs
                  </td>
                  <td className="px-4 py-2.5 text-gray-600">
                    ${e.accumulated_cost_usd.toFixed(2)}
                    {e.budget_limit_usd != null && <span className="text-gray-400"> / ${e.budget_limit_usd.toFixed(2)}</span>}
                  </td>
                  <td className="px-4 py-2.5 text-gray-500">{new Date(e.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && <ExperimentForm onClose={() => setShowCreate(false)} />}
    </div>
  )
}
