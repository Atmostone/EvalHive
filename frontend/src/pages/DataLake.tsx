import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Database, Download, X } from 'lucide-react'
import { dataLakeApi } from '@/api/client'
import type { DataLakeFilters } from '@/api/client'

// E-01 Data Lake browser: the immutable execution-record corpus that feeds every
// downstream judge. Endpoints existed (records / query / export) with no UI.
type View = 'records' | 'group'
const GROUP_BYS = ['template_name', 'model_used', 'final_status'] as const

function fmt(n: number | null | undefined, digits = 2): string {
  return n == null ? '—' : n.toFixed(digits)
}

function RawRecordModal({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ['data-lake-record', taskId],
    queryFn: () => dataLakeApi.getRecord(taskId),
  })
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl w-full max-w-3xl max-h-[88vh] overflow-y-auto p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold">Immutable record snapshot</h2>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-100"><X className="h-5 w-5" /></button>
        </div>
        <p className="text-xs text-gray-400 mb-2 font-mono">{taskId}</p>
        {isLoading ? (
          <div className="text-sm text-gray-500 p-4">Loading snapshot…</div>
        ) : data?.record == null ? (
          <div className="text-sm text-gray-500 p-4">
            No immutable blob captured for this record{data?.summary.record_s3_path ? '' : ' (no S3 path)'}.
          </div>
        ) : (
          <pre className="text-xs bg-gray-50 border rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">
            {JSON.stringify(data.record, null, 2)}
          </pre>
        )}
      </div>
    </div>
  )
}

export default function DataLake() {
  const [view, setView] = useState<View>('records')
  const [groupBy, setGroupBy] = useState<(typeof GROUP_BYS)[number]>('model_used')
  const [filters, setFilters] = useState<DataLakeFilters>({})
  const [rawTask, setRawTask] = useState<string | null>(null)

  const records = useQuery({
    queryKey: ['data-lake-records', filters],
    queryFn: () => dataLakeApi.list({ ...filters, limit: 200 }),
    enabled: view === 'records',
  })
  const grouped = useQuery({
    queryKey: ['data-lake-query', groupBy, filters],
    queryFn: () => dataLakeApi.query(groupBy, filters),
    enabled: view === 'group',
  })

  const setFilter = (k: keyof DataLakeFilters, v: string) =>
    setFilters((p) => ({ ...p, [k]: v || undefined }))

  const download = async () => {
    const blob = await dataLakeApi.export('json', filters)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'data-lake-records.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
          <Database className="h-6 w-6" /> Data Lake
        </h1>
        <button onClick={download} title="Export the filtered summary table as JSON"
          className="flex items-center gap-1.5 px-3 py-2 border rounded-lg hover:bg-gray-50 text-sm">
          <Download className="h-4 w-4" /> Export JSON
        </button>
      </div>
      <p className="text-sm text-gray-500">
        Иммутабельный корпус записей о прогонах — то, на чём учатся все судьи. Фильтры сужают популяцию;
        «Group by» даёт кросс-разрез по модели/шаблону/статусу.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <input value={filters.title_contains || ''} onChange={(e) => setFilter('title_contains', e.target.value)}
          placeholder="title contains…" className="px-3 py-1.5 border rounded-lg text-sm" />
        <input value={filters.model_used || ''} onChange={(e) => setFilter('model_used', e.target.value)}
          placeholder="model (e.g. glm-4.7)" className="px-3 py-1.5 border rounded-lg text-sm" />
        <input value={filters.final_status || ''} onChange={(e) => setFilter('final_status', e.target.value)}
          placeholder="status (e.g. done)" className="px-3 py-1.5 border rounded-lg text-sm" />
        <div className="flex border rounded-lg overflow-hidden ml-auto text-sm">
          {(['records', 'group'] as View[]).map((v) => (
            <button key={v} onClick={() => setView(v)}
              className={`px-3 py-1.5 ${view === v ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              {v === 'records' ? 'Records' : 'Group by'}
            </button>
          ))}
        </div>
        {view === 'group' && (
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as (typeof GROUP_BYS)[number])}
            className="px-3 py-1.5 border rounded-lg text-sm bg-white">
            {GROUP_BYS.map((g) => <option key={g} value={g}>{g.replace(/_/g, ' ')}</option>)}
          </select>
        )}
      </div>

      {view === 'records' && (
        <div className="bg-white border rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-3 py-2">Created</th>
                <th className="px-3 py-2">Template</th>
                <th className="px-3 py-2">Model</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2 text-right">Cost</th>
                <th className="px-3 py-2 text-right">Tokens</th>
                <th className="px-3 py-2 text-right">Time</th>
                <th className="px-3 py-2 text-right">Tools</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {(records.data || []).map((r) => (
                <tr key={r.task_id} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2 text-gray-500 whitespace-nowrap">{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
                  <td className="px-3 py-2">{r.template_name || '—'}</td>
                  <td className="px-3 py-2">{r.model_used || '—'}</td>
                  <td className="px-3 py-2">{r.final_status || '—'}</td>
                  <td className="px-3 py-2 text-right">{r.cost_usd == null ? '—' : `$${fmt(r.cost_usd, 3)}`}</td>
                  <td className="px-3 py-2 text-right text-gray-600">{(r.input_tokens ?? 0) + (r.output_tokens ?? 0)}</td>
                  <td className="px-3 py-2 text-right text-gray-600">{r.duration_seconds != null ? `${Math.round(r.duration_seconds)}s` : '—'}</td>
                  <td className="px-3 py-2 text-right text-gray-600">{r.tool_call_count ?? '—'}</td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => setRawTask(r.task_id)}
                      className="text-xs px-2 py-0.5 rounded border text-gray-600 hover:bg-blue-50 hover:border-blue-400 hover:text-blue-700">
                      raw
                    </button>
                  </td>
                </tr>
              ))}
              {records.data && records.data.length === 0 && (
                <tr><td colSpan={9} className="px-3 py-8 text-center text-gray-400">No records match these filters</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {view === 'group' && (
        <div className="bg-white border rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-3 py-2">{groupBy.replace(/_/g, ' ')}</th>
                <th className="px-3 py-2 text-right">Count</th>
                <th className="px-3 py-2 text-right">Approval</th>
                <th className="px-3 py-2 text-right">Avg cost</th>
                <th className="px-3 py-2 text-right">Avg tokens</th>
                <th className="px-3 py-2 text-right">Avg time</th>
              </tr>
            </thead>
            <tbody>
              {(grouped.data || []).map((g, i) => (
                <tr key={`${g.group}-${i}`} className="border-t">
                  <td className="px-3 py-2 font-medium">{g.group ?? '—'}</td>
                  <td className="px-3 py-2 text-right">{g.count}</td>
                  <td className="px-3 py-2 text-right">{(g.approval_rate * 100).toFixed(0)}%</td>
                  <td className="px-3 py-2 text-right" title={`Reported for ${g.cost_known_count} of ${g.count} runs`}>{g.avg_cost_usd == null ? '—' : `$${fmt(g.avg_cost_usd, 3)}`}</td>
                  <td className="px-3 py-2 text-right text-gray-600" title={`Reported for ${g.tokens_known_count} of ${g.count} runs`}>{g.avg_tokens == null ? '—' : Math.round(g.avg_tokens)}</td>
                  <td className="px-3 py-2 text-right text-gray-600">{g.avg_duration_s == null ? '—' : `${Math.round(g.avg_duration_s)}s`}</td>
                </tr>
              ))}
              {grouped.data && grouped.data.length === 0 && (
                <tr><td colSpan={6} className="px-3 py-8 text-center text-gray-400">No records match these filters</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {rawTask && <RawRecordModal taskId={rawTask} onClose={() => setRawTask(null)} />}
    </div>
  )
}
