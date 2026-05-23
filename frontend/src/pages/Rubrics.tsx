import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { rubricsApi } from '@/api/client'
import type { Rubric, RubricDimension, EvaluatorType, ReferenceMode } from '@/types'
import { Plus, Edit2, Trash2, X, Gauge, Star } from 'lucide-react'

const EVALUATORS: EvaluatorType[] = ['judge', 'reference', 'objective', 'human']
const READY_EVALUATORS: EvaluatorType[] = ['judge', 'reference']
const REFERENCE_MODES: ReferenceMode[] = ['pointwise', 'exact', 'fuzzy', 'semantic']

function emptyDimension(): RubricDimension {
  return { key: '', name: '', description: '', evaluator: 'judge', weight: 1, threshold: 5, critical: false }
}

function RubricForm({ rubric, onClose }: { rubric?: Rubric; onClose: () => void }) {
  const queryClient = useQueryClient()
  const isEdit = !!rubric

  const [name, setName] = useState(rubric?.name || '')
  const [description, setDescription] = useState(rubric?.description || '')
  const [appliesTo, setAppliesTo] = useState(rubric?.applies_to || '')
  const [isDefault, setIsDefault] = useState(rubric?.is_default || false)
  const [dimensions, setDimensions] = useState<RubricDimension[]>(
    rubric?.dimensions?.length ? rubric.dimensions.map((d) => ({ ...d })) : [emptyDimension()],
  )

  const payload = () => ({
    name: name.trim(),
    description: description.trim(),
    applies_to: appliesTo.trim() || null,
    is_default: isDefault,
    dimensions: dimensions
      .filter((d) => d.key.trim() && d.name.trim())
      .map((d) => ({ ...d, key: d.key.trim(), name: d.name.trim() })),
  })

  const createMutation = useMutation({
    mutationFn: () => rubricsApi.create(payload()),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['rubrics'] }); onClose() },
  })
  const updateMutation = useMutation({
    mutationFn: () => rubricsApi.update(rubric!.id, payload()),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['rubrics'] }); onClose() },
  })

  const setDim = (idx: number, patch: Partial<RubricDimension>) =>
    setDimensions((prev) => prev.map((d, i) => (i === idx ? { ...d, ...patch } : d)))

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">{isEdit ? 'Edit Rubric' : 'New Rubric'}</h2>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-100"><X className="h-5 w-5" /></button>
        </div>

        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="e.g. Code" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Applies to tag</label>
              <input value={appliesTo} onChange={(e) => setAppliesTo(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" placeholder="e.g. coding (template tag)" />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm h-14 resize-none" />
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
            Default rubric (used when no template/tag matches)
          </label>

          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium text-gray-700">Dimensions</label>
              <button type="button" onClick={() => setDimensions((p) => [...p, emptyDimension()])}
                className="text-xs px-2 py-1 border rounded hover:bg-gray-50">+ Add dimension</button>
            </div>
            {dimensions.map((d, idx) => (
              <div key={idx} className="border rounded-lg p-3 mb-2 bg-gray-50 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-gray-500">Dimension #{idx + 1}</span>
                  <button type="button" onClick={() => setDimensions((p) => p.filter((_, i) => i !== idx))}
                    className="text-xs text-red-500 hover:underline">remove</button>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <input placeholder="key (e.g. correctness)" value={d.key}
                    onChange={(e) => setDim(idx, { key: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white" />
                  <input placeholder="name (e.g. Correctness)" value={d.name}
                    onChange={(e) => setDim(idx, { name: e.target.value })}
                    className="px-2 py-1.5 border rounded text-sm bg-white" />
                </div>
                <input placeholder={d.evaluator === 'reference' ? 'what it measures (optional)' : 'what it measures (judge criteria)'} value={d.description}
                  onChange={(e) => setDim(idx, { description: e.target.value })}
                  className="w-full px-2 py-1.5 border rounded text-sm bg-white" />
                <div className="grid grid-cols-4 gap-2 items-center">
                  <select value={d.evaluator} onChange={(e) => {
                    const evaluator = e.target.value as EvaluatorType
                    setDim(idx, {
                      evaluator,
                      reference_mode: evaluator === 'reference' ? (d.reference_mode || 'pointwise') : null,
                    })
                  }}
                    className="px-2 py-1.5 border rounded text-sm bg-white">
                    {EVALUATORS.map((ev) => (
                      <option key={ev} value={ev}>{ev}{READY_EVALUATORS.includes(ev) ? '' : ' (soon)'}</option>
                    ))}
                  </select>
                  <input type="number" step="0.05" min={0} placeholder="weight" value={d.weight}
                    onChange={(e) => setDim(idx, { weight: Number(e.target.value) })}
                    className="px-2 py-1.5 border rounded text-sm bg-white" title="weight" />
                  <input type="number" min={0} max={10} placeholder="threshold" value={d.threshold ?? ''}
                    onChange={(e) => setDim(idx, { threshold: e.target.value === '' ? null : Number(e.target.value) })}
                    className="px-2 py-1.5 border rounded text-sm bg-white" title="threshold (0-10)" />
                  <label className="flex items-center gap-1 text-xs text-gray-600">
                    <input type="checkbox" checked={d.critical}
                      onChange={(e) => setDim(idx, { critical: e.target.checked })} />
                    critical
                  </label>
                </div>
                {d.evaluator === 'reference' && (
                  <div className="flex items-center gap-2">
                    <label className="text-xs text-gray-500 whitespace-nowrap">reference mode</label>
                    <select value={d.reference_mode || 'pointwise'}
                      onChange={(e) => setDim(idx, { reference_mode: e.target.value as ReferenceMode })}
                      className="px-2 py-1.5 border rounded text-sm bg-white" title="compares the result against the task's reference answer">
                      {REFERENCE_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                    <span className="text-xs text-gray-400">compares result vs the task's reference answer</span>
                  </div>
                )}
              </div>
            ))}
          </div>

          <button onClick={() => (isEdit ? updateMutation : createMutation).mutate()}
            disabled={!name.trim() || payload().dimensions.length === 0}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium">
            {isEdit ? 'Save Changes' : 'Create Rubric'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Rubrics() {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<Rubric | undefined>()
  const [showCreate, setShowCreate] = useState(false)

  const { data: rubrics = [] } = useQuery({ queryKey: ['rubrics'], queryFn: rubricsApi.list })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => rubricsApi.remove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['rubrics'] }),
  })

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-2xl font-bold text-gray-900">Quality Rubrics</h1>
        <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
          <Plus className="h-4 w-4" /> New Rubric
        </button>
      </div>
      <p className="text-sm text-gray-500 mb-6">
        A rubric scores a task result on multiple independent dimensions (0–10), producing a quality
        profile instead of one number. Dimensions are scored by an LLM judge or by comparison against
        a reference answer (objective/human evaluators coming with E-04/E-05).
      </p>

      {rubrics.length === 0 ? (
        <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
          <Gauge className="h-12 w-12 mx-auto mb-3 text-gray-300" />
          <p>No rubrics yet</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {rubrics.map((r) => (
            <div key={r.id} className="bg-white rounded-lg border p-4 hover:shadow-md transition-shadow">
              <div className="flex items-start justify-between mb-2">
                <h3 className="font-semibold text-gray-900 flex items-center gap-1.5">
                  {r.is_default && <Star className="h-4 w-4 text-amber-400 fill-amber-400" />}
                  {r.name}
                </h3>
                <div className="flex gap-1">
                  <button onClick={() => setEditing(r)} className="p-1 rounded hover:bg-gray-100" title="Edit">
                    <Edit2 className="h-4 w-4 text-gray-400" />
                  </button>
                  <button onClick={() => { if (confirm('Delete this rubric?')) deleteMutation.mutate(r.id) }}
                    className="p-1 rounded hover:bg-red-50" title="Delete">
                    <Trash2 className="h-4 w-4 text-gray-400 hover:text-red-500" />
                  </button>
                </div>
              </div>
              {r.description && <p className="text-sm text-gray-500 mb-3 line-clamp-2">{r.description}</p>}
              <div className="flex flex-wrap gap-1.5">
                <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">
                  {r.dimensions.length} dimensions
                </span>
                {r.applies_to && (
                  <span className="text-xs px-2 py-0.5 rounded-full bg-blue-100 text-blue-700">
                    tag: {r.applies_to}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {(showCreate || editing) && (
        <RubricForm rubric={editing} onClose={() => { setShowCreate(false); setEditing(undefined) }} />
      )}
    </div>
  )
}
