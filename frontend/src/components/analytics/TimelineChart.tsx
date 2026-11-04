import { useMemo } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { format, parseISO } from 'date-fns'
import type { TimelinePoint } from '@/api/client'

export default function TimelineChart({ data }: { data: TimelinePoint[] }) {
  const chartData = useMemo(
    () =>
      data
        .filter((p) => p.date)
        .map((p) => ({
          date: p.date as string,
          label: format(parseISO(p.date as string), 'MMM d'),
          tasks: p.task_count,
          cost: p.total_cost_usd === null ? null : Number(p.total_cost_usd.toFixed(4)),
        })),
    [data],
  )

  if (chartData.length === 0) {
    return (
      <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
        No tasks in this period
      </div>
    )
  }

  return (
    <div className="bg-white rounded-lg border p-4">
      <h3 className="text-sm font-semibold text-gray-700 mb-3">Tasks &amp; cost over time</h3>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData} margin={{ top: 10, right: 20, left: 0, bottom: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis dataKey="label" tick={{ fontSize: 11 }} />
          <YAxis
            yAxisId="left"
            tick={{ fontSize: 11 }}
            label={{ value: 'Tasks', angle: -90, position: 'insideLeft', style: { fontSize: 11 } }}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={{ fontSize: 11 }}
            label={{ value: 'Cost ($)', angle: 90, position: 'insideRight', style: { fontSize: 11 } }}
          />
          <Tooltip />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line yAxisId="left" type="monotone" dataKey="tasks" name="Tasks" stroke="#2563eb" strokeWidth={2} dot={{ r: 3 }} />
          <Line yAxisId="right" type="monotone" dataKey="cost" name="Cost ($)" stroke="#f59e0b" strokeWidth={2} dot={{ r: 3 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
