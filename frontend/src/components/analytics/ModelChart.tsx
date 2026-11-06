import { useMemo } from 'react'
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
import type { ModelAnalytics } from '@/api/client'

export default function ModelChart({ data }: { data: ModelAnalytics[] }) {
  const chartData = useMemo(
    () =>
      data.map((m) => ({
        name: m.model.length > 20 ? `${m.model.slice(0, 18)}…` : m.model,
        cost: m.total_cost_usd === null ? null : Number(m.total_cost_usd.toFixed(4)),
        tasks: m.task_count,
      })),
    [data],
  )

  if (chartData.length === 0) {
    return (
      <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
        No model data in this period
      </div>
    )
  }

  return (
    <div className="bg-white rounded-lg border p-4">
      <h3 className="text-sm font-semibold text-gray-700 mb-3">Cost &amp; task volume by model</h3>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={chartData} margin={{ top: 10, right: 20, left: 0, bottom: 30 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis dataKey="name" angle={-20} textAnchor="end" height={50} tick={{ fontSize: 11 }} />
          <YAxis
            yAxisId="left"
            tick={{ fontSize: 11 }}
            label={{ value: 'Cost ($)', angle: -90, position: 'insideLeft', style: { fontSize: 11 } }}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={{ fontSize: 11 }}
            label={{ value: 'Tasks', angle: 90, position: 'insideRight', style: { fontSize: 11 } }}
          />
          <Tooltip />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Bar yAxisId="left" dataKey="cost" name="Cost ($)" fill="#7c3aed" />
          <Bar yAxisId="right" dataKey="tasks" name="Tasks" fill="#10b981" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
