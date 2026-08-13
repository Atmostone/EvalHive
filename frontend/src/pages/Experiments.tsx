import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { benchmarksApi, experimentsApi, providersApi, registryApi, templatesApi } from '@/api/client'
import type { ExperimentCreateBody } from '@/api/client'
import type { Experiment, ExperimentStatus, LLMModel, RegistryEntry } from '@/types'
import { FlaskConical, Plus, Trash2, X } from 'lucide-react'

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
  tools_enable: string[]
  tools_disable: string[]
}

function emptyConfig(): ConfigDraft {
  return { label: '', orchestrator: false, template_id: '', model_id: '', temperature: '', seed: '', soul_md: '', memory_mode: '', tools_enable: [], tools_disable: [] }
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
  if (!c.orchestrator && (c.tools_enable.length || c.tools_disable.length)) {
    const ov: Record<string, string[]> = {}
    if (c.tools_enable.length) ov.enable = c.tools_enable
    if (c.tools_disable.length) ov.disable = c.tools_disable
    out.tools_override = ov
  }
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
  const [configMode, setConfigMode] = useState<'manual' | 'axes'>('manual')
  const [axTemplates, setAxTemplates] = useState<string[]>([])
  const [axModels, setAxModels] = useState<string[]>([])
  const [axTemps, setAxTemps] = useState('')
  const [axMemory, setAxMemory] = useState<string[]>([])
  const [nRuns, setNRuns] = useState(3)
  const [budget, setBudget] = useState('')
  const [maxParallel, setMaxParallel] = useState('')
  const [lanes, setLanes] = useState('')
  // Evaluation mode: 'checker' = run the executable checker where a case has one
  // (Toolathlon ground truth). 'judge' = skip the checker and let the E-02 outcome
  // judge be the evaluator — turns a verifiable bench into an open-result one so
  // the outcome×trajectory view works where there is no oracle. (SPA-56/E-25)
  const [evalMode, setEvalMode] = useState<'checker' | 'judge'>('checker')
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
  const { data: tools = [] } = useQuery({ queryKey: ['registry-tools'], queryFn: () => registryApi.list() })
  const { data: suites = [] } = useQuery({ queryKey: ['benchmark-suites'], queryFn: () => benchmarksApi.listSuites() })
  const { data: suiteDetail } = useQuery({
    queryKey: ['benchmark-suite', suite],
    queryFn: () => benchmarksApi.getSuite(suite),
    enabled: source === 'benchmark_suite' && suite.trim() !== '',
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

  const axes = useMemo(() => {
    const a: Record<string, unknown[]> = {}
    if (axTemplates.length) a.template_id = axTemplates
    if (axModels.length) a.model_id = axModels
    const temps = axTemps.split(',').map((s) => s.trim()).filter(Boolean).map(Number).filter((n) => !Number.isNaN(n))
    if (temps.length) a.temperature = temps
    if (axMemory.length) a.memory_mode = axMemory
    return a
  }, [axTemplates, axModels, axTemps, axMemory])

  const body: ExperimentCreateBody = useMemo(
    () => ({
      name: name.trim(),
      description: description.trim() || null,
      dataset,
      configurations: configMode === 'axes' ? [] : configs.map(configToPayload),
      axes: configMode === 'axes' ? axes : undefined,
      n_runs_per_cell: nRuns,
      budget_limit_usd: budget !== '' ? Number(budget) : null,
      max_parallel: maxParallel !== '' ? Number(maxParallel) : null,
      n_toolathlon_lanes: lanes !== '' ? Number(lanes) : null,
      eval_config: { eval_mode: evalMode },
    }),
    [name, description, dataset, configMode, configs, axes, nRuns, budget, maxParallel, lanes, evalMode],
  )

  const datasetReady =
    (source === 'upload' && uploadCases.length > 0 && uploadErrors.length === 0) ||
    (source === 'benchmark_suite' && suite.trim() !== '') ||
    (source === 'tasks' && (dataset as { task_ids?: string[] }).task_ids!.length > 0)
  const configsReady =
    configMode === 'axes'
      ? axTemplates.length > 0 // template axis is required (combos run orchestrator-off)
      : configs.every((c) => c.orchestrator || c.template_id)
  // What's still missing, in plain language — surfaced next to the disabled Create
  // button so it never just sits greyed-out with no reason why. (SPA-74)
  const blockers: string[] = []
  if (name.trim() === '') blockers.push('a name')
  if (!datasetReady) {
    if (source === 'upload') {
      blockers.push(uploadErrors.length ? 'a valid JSONL dataset (fix the errors above)' : 'at least one dataset case')
    } else if (source === 'benchmark_suite') {
      blockers.push('a benchmark suite')
    } else {
      blockers.push('at least one task ID')
    }
  }
  if (!configsReady) {
    blockers.push(configMode === 'axes'
      ? 'a template in the axes grid'
      : 'a template for every manual config (or orchestrator on)')
  }
  if (nRuns < 1) blockers.push('runs per cell ≥ 1')
  const ready = blockers.length === 0

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
              <label htmlFor="exp-name" className="block text-sm font-medium text-gray-700 mb-1">
                Name <span className="text-red-500" title="required">*</span>
              </label>
              <input id="exp-name" value={name} onChange={(e) => setName(e.target.value)}
                aria-required="true"
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="MiniMax vs DeepSeek on Researcher" />
            </div>
            <div>
              <label htmlFor="exp-description" className="block text-sm font-medium text-gray-700 mb-1">
                Description <span className="text-gray-400 font-normal">(optional)</span>
              </label>
              <input id="exp-description" value={description} onChange={(e) => setDescription(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="optional" />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Dataset <span className="text-red-500" title="required">*</span>
            </label>
            <div className="flex gap-2 mb-2" role="group" aria-label="Dataset source">
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
              <div className="space-y-2">
                <select value={suite} onChange={(e) => setSuite(e.target.value)}
                  className="w-full px-3 py-2 border rounded-lg text-sm bg-white">
                  <option value="">select a suite…</option>
                  {suites.map((s) => (
                    <option key={s.name} value={s.name}>{s.name} ({s.n_cases} cases)</option>
                  ))}
                </select>
                {suiteDetail && suiteDetail.cases.length > 0 && (
                  <details className="text-xs border rounded-lg bg-gray-50">
                    <summary className="cursor-pointer select-none px-3 py-2 text-gray-600">
                      {suiteDetail.n_cases} cases — inspect gold signals
                    </summary>
                    <div className="max-h-48 overflow-y-auto px-3 pb-2 space-y-1">
                      {suiteDetail.cases.map((c) => (
                        <div key={c.id} className="flex items-center justify-between gap-2 bg-white rounded border px-2 py-1">
                          <span className="truncate text-gray-700" title={c.title}>
                            {c.id} <span className="text-gray-400">{c.family || c.category}</span>
                          </span>
                          <span className="flex gap-1 shrink-0 text-[10px]">
                            {c.gold.reference_answer && <span className="px-1 rounded bg-blue-100 text-blue-700" title="reference_answer">ref</span>}
                            {c.gold.rubric && <span className="px-1 rounded bg-purple-100 text-purple-700" title="rubric">rub</span>}
                            {c.gold.canonical_trajectory && <span className="px-1 rounded bg-green-100 text-green-700" title="canonical_trajectory">traj</span>}
                            {c.gold.capability_spec && <span className="px-1 rounded bg-amber-100 text-amber-700" title="capability_spec">cap</span>}
                            {c.gold.external_eval && <span className="px-1 rounded bg-rose-100 text-rose-700" title="external_eval (executable checker)">exec</span>}
                          </span>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </div>
            )}
            {source === 'tasks' && (
              <textarea value={taskIds} onChange={(e) => setTaskIds(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-xs font-mono h-20"
                placeholder="task UUIDs, comma or newline separated" />
            )}
          </div>

          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium text-gray-700">
                Configurations (A/B matrix) <span className="text-red-500" title="required">*</span>
              </label>
              <div className="flex items-center gap-2">
                <div className="flex border rounded-lg overflow-hidden text-xs">
                  {(['manual', 'axes'] as const).map((m) => (
                    <button key={m} type="button" onClick={() => setConfigMode(m)}
                      className={`px-2.5 py-1 ${configMode === m ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
                      {m === 'manual' ? 'Manual cards' : 'Axes grid'}
                    </button>
                  ))}
                </div>
                {configMode === 'manual' && (
                  <button type="button" onClick={() => setConfigs((p) => [...p, emptyConfig()])}
                    className="text-xs px-2 py-1 border rounded hover:bg-gray-50">+ Add configuration</button>
                )}
              </div>
            </div>
            {configMode === 'axes' && (
              <div className="border rounded-lg p-3 mb-2 bg-gray-50 space-y-3 text-sm">
                <p className="text-xs text-gray-500">
                  Auto cross-product over axis value-lists (orchestrator off). The matrix = templates × models × temperatures × memory.
                  <span className="text-gray-400"> Template is required.</span>
                </p>
                <div>
                  <div className="text-xs font-medium text-gray-600 mb-1">Templates <span className="text-gray-400">(required)</span></div>
                  <div className="flex flex-wrap gap-1.5">
                    {templates.map((t) => {
                      const on = axTemplates.includes(t.id)
                      return (
                        <button key={t.id} type="button"
                          onClick={() => setAxTemplates((p) => on ? p.filter((x) => x !== t.id) : [...p, t.id])}
                          className={`px-2 py-1 rounded border text-xs ${on ? 'bg-blue-100 border-blue-400 text-blue-700' : 'bg-white text-gray-600 hover:bg-gray-100'}`}>
                          {t.name}
                        </button>
                      )
                    })}
                  </div>
                </div>
                <div>
                  <div className="text-xs font-medium text-gray-600 mb-1">Models</div>
                  <div className="flex flex-wrap gap-1.5 max-h-28 overflow-y-auto">
                    {models.map((m) => {
                      const on = axModels.includes(m.id)
                      return (
                        <button key={m.id} type="button"
                          onClick={() => setAxModels((p) => on ? p.filter((x) => x !== m.id) : [...p, m.id])}
                          className={`px-2 py-1 rounded border text-xs ${on ? 'bg-blue-100 border-blue-400 text-blue-700' : 'bg-white text-gray-600 hover:bg-gray-100'}`}>
                          {m.display_name}
                        </button>
                      )
                    })}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <div className="text-xs font-medium text-gray-600 mb-1">Temperatures <span className="text-gray-400">(comma-sep)</span></div>
                    <input value={axTemps} onChange={(e) => setAxTemps(e.target.value)} placeholder="e.g. 0, 0.7, 1"
                      className="w-full px-2 py-1.5 border rounded text-sm bg-white" />
                  </div>
                  <div>
                    <div className="text-xs font-medium text-gray-600 mb-1">Memory modes</div>
                    <div className="flex flex-wrap gap-1.5">
                      {(['off', 'flat', 'structured'] as const).map((mm) => {
                        const on = axMemory.includes(mm)
                        return (
                          <button key={mm} type="button"
                            onClick={() => setAxMemory((p) => on ? p.filter((x) => x !== mm) : [...p, mm])}
                            className={`px-2 py-1 rounded border text-xs ${on ? 'bg-blue-100 border-blue-400 text-blue-700' : 'bg-white text-gray-600 hover:bg-gray-100'}`}>
                            {mm}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                </div>
              </div>
            )}
            {configMode === 'manual' && configs.map((c, idx) => (
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
                {!c.orchestrator && tools.length > 0 && (
                  <details className="text-xs">
                    <summary className="cursor-pointer select-none text-gray-600">
                      Tools override{' '}
                      {c.tools_enable.length + c.tools_disable.length > 0 ? (
                        <span className="text-gray-700 font-medium">
                          ({c.tools_enable.length}+ / {c.tools_disable.length}−)
                        </span>
                      ) : (
                        <span className="text-gray-400">— template default</span>
                      )}
                    </summary>
                    <div className="mt-2 space-y-1 max-h-44 overflow-y-auto pr-1">
                      {tools.map((t: RegistryEntry) => {
                        const on = c.tools_enable.includes(t.id)
                        const off = c.tools_disable.includes(t.id)
                        const set = (mode: 'default' | 'enable' | 'disable') =>
                          setConfig(idx, {
                            tools_enable:
                              mode === 'enable'
                                ? [...c.tools_enable.filter((x) => x !== t.id), t.id]
                                : c.tools_enable.filter((x) => x !== t.id),
                            tools_disable:
                              mode === 'disable'
                                ? [...c.tools_disable.filter((x) => x !== t.id), t.id]
                                : c.tools_disable.filter((x) => x !== t.id),
                          })
                        return (
                          <div key={t.id} className="flex items-center justify-between gap-2 bg-white rounded border px-2 py-1">
                            <span className="truncate text-gray-700" title={t.name}>
                              {t.name} <span className="text-gray-400">{t.kind}</span>
                            </span>
                            <div className="flex gap-1 shrink-0">
                              <button type="button" onClick={() => set('default')}
                                className={`px-1.5 py-0.5 rounded ${!on && !off ? 'bg-gray-200 text-gray-700' : 'text-gray-400 hover:bg-gray-100'}`}>default</button>
                              <button type="button" onClick={() => set('enable')}
                                className={`px-1.5 py-0.5 rounded ${on ? 'bg-green-100 text-green-700' : 'text-gray-400 hover:bg-gray-100'}`}>+ enable</button>
                              <button type="button" onClick={() => set('disable')}
                                className={`px-1.5 py-0.5 rounded ${off ? 'bg-red-100 text-red-700' : 'text-gray-400 hover:bg-gray-100'}`}>− disable</button>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </details>
                )}
              </div>
            ))}
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div>
              <label htmlFor="exp-nruns" className="block text-sm font-medium text-gray-700 mb-1">
                Runs per cell (N) <span className="text-red-500" title="required">*</span>
              </label>
              <input id="exp-nruns" type="number" min={1} max={20} value={nRuns} aria-required="true"
                onChange={(e) => setNRuns(Number(e.target.value))}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label htmlFor="exp-budget" className="block text-sm font-medium text-gray-700 mb-1">Budget limit (USD)</label>
              <input id="exp-budget" type="number" step="0.01" min={0} value={budget} placeholder="no limit"
                onChange={(e) => setBudget(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label htmlFor="exp-maxparallel" className="block text-sm font-medium text-gray-700 mb-1">Max parallel</label>
              <input id="exp-maxparallel" type="number" min={1} max={10} value={maxParallel} placeholder="auto"
                onChange={(e) => setMaxParallel(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label htmlFor="exp-lanes" className="block text-sm font-medium text-gray-700 mb-1">Toolathlon lanes</label>
              <input id="exp-lanes" type="number" min={1} max={4} value={lanes} placeholder="serial"
                title="Isolated Postgres lanes for PARALLEL Toolathlon runs (max 4). Each lane is its own DB so concurrent cases can't clobber each other's seed. Blank = serial on the shared toolathlon_pg. No effect on non-Toolathlon datasets."
                onChange={(e) => setLanes(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Evaluation mode</label>
            <div className="flex border rounded-lg overflow-hidden w-fit text-sm">
              {(['checker', 'judge'] as const).map((m) => (
                <button key={m} type="button" onClick={() => setEvalMode(m)}
                  className={`px-3 py-1.5 ${evalMode === m ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
                  {m === 'checker' ? 'Checker (executable)' : 'Judge (LLM, no checker)'}
                </button>
              ))}
            </div>
            <p className="text-xs text-gray-500 mt-1">
              {evalMode === 'checker'
                ? 'Cases with an executable checker (e.g. Toolathlon) are graded by ground truth; the outcome judge is skipped there.'
                : 'Skip the executable checker — the outcome judge becomes the evaluator (open-result mode). Use to exercise the judge + outcome×trajectory view where there is no oracle. Preprocess still seeds the environment.'}
            </p>
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
            <div className="text-red-700 text-xs bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              ⚠ Preview failed: {(previewError as Error).message} — you can still create the draft, but double-check the configuration.
            </div>
          )}
          {submitError && <div className="text-red-500 text-xs whitespace-pre-wrap">{submitError}</div>}

          {!ready && (
            <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
              To create, add: {blockers.join(' · ')}.
            </div>
          )}
          <button onClick={() => { setSubmitError(''); createMutation.mutate() }}
            disabled={!ready || createMutation.isPending}
            title={ready ? undefined : `Missing: ${blockers.join(', ')}`}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium">
            {createMutation.isPending ? 'Creating…' : 'Create Experiment (draft)'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Experiments() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)

  const { data: experiments = [] } = useQuery({
    queryKey: ['experiments'],
    queryFn: () => experimentsApi.list(),
    refetchInterval: 5000,
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => experimentsApi.remove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['experiments'] }),
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
                <th className="px-4 py-2"></th>
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
                    {/* SPA-84: an id alone no longer pins a fixed set of runs. */}
                    {e.revision > 1 && (
                      <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-amber-100 text-amber-700 font-medium"
                        title={`Revision ${e.revision} — mutated ${e.revision - 1} time(s) since creation${e.n_retired_configs ? `, ${e.n_retired_configs} configuration(s) retired` : ''}.`}>
                        rev {e.revision}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-gray-600">
                    ${e.accumulated_cost_usd.toFixed(2)}
                    {e.budget_limit_usd != null && <span className="text-gray-400"> / ${e.budget_limit_usd.toFixed(2)}</span>}
                  </td>
                  <td className="px-4 py-2.5 text-gray-500">{new Date(e.created_at).toLocaleString()}</td>
                  <td className="px-4 py-2.5 text-right">
                    {e.status !== 'running' && (
                      <button
                        onClick={(ev) => {
                          ev.stopPropagation()
                          if (confirm(`Delete experiment "${e.name}"? This cannot be undone.`)) deleteMutation.mutate(e.id)
                        }}
                        title="Delete experiment"
                        className="p-1.5 rounded text-gray-400 hover:text-red-600 hover:bg-red-50">
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </td>
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
