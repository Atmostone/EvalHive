import { useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import type { TemplateAnalytics } from '@/api/client'
import { formatCost, formatDuration, formatPercent, formatTokens } from './format'

type SortKey = keyof TemplateAnalytics

interface Column {
  key: SortKey
  label: string
  align?: 'left' | 'right'
  render: (row: TemplateAnalytics) => string
}

const COLUMNS: Column[] = [
  { key: 'template_name', label: 'Template', align: 'left', render: (r) => r.template_name },
  { key: 'task_count', label: 'Tasks', align: 'right', render: (r) => String(r.task_count) },
  { key: 'approval_rate', label: 'Approval %', align: 'right', render: (r) => formatPercent(r.approval_rate) },
  { key: 'retry_rate', label: 'Retry %', align: 'right', render: (r) => formatPercent(r.retry_rate) },
  { key: 'failure_rate', label: 'Failure %', align: 'right', render: (r) => formatPercent(r.failure_rate) },
  { key: 'avg_time_seconds', label: 'Avg Time', align: 'right', render: (r) => formatDuration(r.avg_time_seconds) },
  { key: 'avg_input_tokens', label: 'Avg In', align: 'right', render: (r) => formatTokens(r.avg_input_tokens) },
  { key: 'avg_output_tokens', label: 'Avg Out', align: 'right', render: (r) => formatTokens(r.avg_output_tokens) },
  { key: 'cost_per_task_usd', label: 'Cost / Task', align: 'right', render: (r) => formatCost(r.cost_per_task_usd) },
  { key: 'total_cost_usd', label: 'Total Cost', align: 'right', render: (r) => formatCost(r.total_cost_usd) },
]

export default function TemplateMetricsTable({ data }: { data: TemplateAnalytics[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('task_count')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  const sorted = useMemo(() => {
    const copy = [...data]
    copy.sort((a, b) => {
      const va = a[sortKey]
      const vb = b[sortKey]
      if (typeof va === 'string' && typeof vb === 'string') {
        return sortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va)
      }
      const na = Number(va) || 0
      const nb = Number(vb) || 0
      return sortDir === 'asc' ? na - nb : nb - na
    })
    return copy
  }, [data, sortKey, sortDir])

  const toggle = (k: SortKey) => {
    if (k === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(k)
      setSortDir(k === 'template_name' ? 'asc' : 'desc')
    }
  }

  const chartData = useMemo(
    () =>
      sorted.map((r) => ({
        name: r.template_name.length > 16 ? `${r.template_name.slice(0, 14)}…` : r.template_name,
        approval_pct: Number((r.approval_rate * 100).toFixed(1)),
        cost_per_task: r.cost_per_task_usd === null ? null : Number(r.cost_per_task_usd.toFixed(4)),
      })),
    [sorted],
  )

  if (data.length === 0) {
    return (
      <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
        No template data in this period
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg border overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                {COLUMNS.map((c) => (
                  <th
                    key={c.key}
                    className={`px-3 py-2 font-medium text-gray-700 cursor-pointer select-none ${
                      c.align === 'right' ? 'text-right' : 'text-left'
                    }`}
                    onClick={() => toggle(c.key)}
                  >
                    <span className="inline-flex items-center gap-1">
                      {c.label}
                      {sortKey === c.key ? (
                        sortDir === 'asc' ? (
                          <ArrowUp className="h-3 w-3" />
                        ) : (
                          <ArrowDown className="h-3 w-3" />
                        )
                      ) : (
                        <ArrowUpDown className="h-3 w-3 text-gray-300" />
                      )}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((row) => (
                <tr key={row.template_id} className="border-b last:border-0 hover:bg-gray-50">
                  {COLUMNS.map((c) => (
                    <td
                      key={c.key}
                      className={`px-3 py-2 ${c.align === 'right' ? 'text-right tabular-nums' : ''}`}
                    >
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="bg-white rounded-lg border p-4">
        <h3 className="text-sm font-semibold text-gray-700 mb-3">Approval rate vs cost per task</h3>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 30 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
            <XAxis dataKey="name" angle={-20} textAnchor="end" height={50} tick={{ fontSize: 11 }} />
            <YAxis
              yAxisId="left"
              tick={{ fontSize: 11 }}
              label={{ value: 'Approval %', angle: -90, position: 'insideLeft', style: { fontSize: 11 } }}
            />
            <YAxis
              yAxisId="right"
              orientation="right"
              tick={{ fontSize: 11 }}
              label={{ value: 'Cost / task ($)', angle: 90, position: 'insideRight', style: { fontSize: 11 } }}
            />
            <Tooltip />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar yAxisId="left" dataKey="approval_pct" name="Approval %" fill="#2563eb" />
            <Bar yAxisId="right" dataKey="cost_per_task" name="Cost / task ($)" fill="#f59e0b" />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
