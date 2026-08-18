import { useState } from 'react'
import { ArrowDown, ArrowUp, Equal } from 'lucide-react'
import type { ConfigAnalytics } from '@/api/client'
import { formatCost, formatDuration, formatPercent } from './format'

type MetricDirection = 'higher_better' | 'lower_better'

interface MetricDef {
  key: keyof ConfigAnalytics
  label: string
  direction: MetricDirection
  format: (v: number) => string
}

const score = (v: number) => v.toFixed(1)

const METRICS: MetricDef[] = [
  { key: 'success_rate', label: 'Success rate', direction: 'higher_better', format: formatPercent },
  { key: 'pass_rate', label: 'Checker pass rate', direction: 'higher_better', format: formatPercent },
  { key: 'quality_mean', label: 'Outcome quality (avg /10)', direction: 'higher_better', format: score },
  { key: 'trajectory_mean', label: 'Trajectory (avg /10)', direction: 'higher_better', format: score },
  { key: 'failure_rate', label: 'Failure rate', direction: 'lower_better', format: formatPercent },
  { key: 'avg_time_seconds', label: 'Avg time', direction: 'lower_better', format: formatDuration },
  { key: 'avg_cost_usd', label: 'Avg cost / run', direction: 'lower_better', format: formatCost },
]

const NEAR_TIE_EPSILON = 0.01

// SPA-87: a configuration whose every run a provider outage killed has NO
// observations. The API sends null for that, and it must stay null all the way
// to the screen — coerced to 0 it silently became «quality 0.0, success 0%» and
// lost every row, which is an outage beating a model in a comparison.
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function compare(
  a: number | null,
  b: number | null,
  direction: MetricDirection,
): 'a' | 'b' | 'tie' | 'incomparable' {
  if (a === null || b === null) return 'incomparable'
  const denom = Math.max(Math.abs(a), Math.abs(b), 1e-9)
  if (Math.abs(a - b) / denom < NEAR_TIE_EPSILON) return 'tie'
  if (direction === 'higher_better') return a > b ? 'a' : 'b'
  return a < b ? 'a' : 'b'
}

function Indicator({ kind }: { kind: 'winner' | 'loser' | 'tie' | 'incomparable' }) {
  if (kind === 'incomparable')
    return (
      <span
        className="h-4 w-4 text-center text-gray-300"
        title="No comparable sample on one side — nothing is being claimed here."
      >
        ·
      </span>
    )
  if (kind === 'tie') return <Equal className="h-4 w-4 text-gray-400" aria-label="approximately equal" />
  if (kind === 'winner') return <ArrowUp className="h-4 w-4 text-green-600" aria-label="better" />
  return <ArrowDown className="h-4 w-4 text-red-500" aria-label="worse" />
}

export default function ConfigCompareView({ data }: { data: ConfigAnalytics[] }) {
  const [aId, setAId] = useState<string>('')
  const [bId, setBId] = useState<string>('')

  const a = data.find((c) => c.config_id === aId)
  const b = data.find((c) => c.config_id === bId)

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg border p-4">
        <p className="text-sm text-gray-600 mb-3">
          Pick two configurations (experiment × config) to compare aggregate metrics side by side.
          Arrows mark the winner per metric direction.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">Config A</label>
            <select
              className="w-full border rounded-lg px-3 py-2 text-sm"
              value={aId}
              onChange={(e) => setAId(e.target.value)}
            >
              <option value="">Select config…</option>
              {data.map((c) => (
                <option key={c.config_id} value={c.config_id}>
                  {c.config_name} ({c.run_count} runs)
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">Config B</label>
            <select
              className="w-full border rounded-lg px-3 py-2 text-sm"
              value={bId}
              onChange={(e) => setBId(e.target.value)}
            >
              <option value="">Select config…</option>
              {data.map((c) => (
                <option key={c.config_id} value={c.config_id}>
                  {c.config_name} ({c.run_count} runs)
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {a && b ? (
        <div className="bg-white rounded-lg border overflow-hidden">
          <div className="grid grid-cols-3 bg-gray-50 border-b text-sm font-medium text-gray-700">
            <div className="px-4 py-3">Metric</div>
            <div className="px-4 py-3 text-center">{a.config_name}</div>
            <div className="px-4 py-3 text-center">{b.config_name}</div>
          </div>
          <div className="grid grid-cols-3 border-b text-sm">
            <div className="px-4 py-2 text-gray-600">Runs observed</div>
            <div className="px-4 py-2 text-center tabular-nums">{a.run_count}</div>
            <div className="px-4 py-2 text-center tabular-nums">{b.run_count}</div>
          </div>
          {(a.contaminated || b.contaminated) ? (
            <div className="grid grid-cols-3 border-b text-sm">
              <div
                className="px-4 py-2 text-gray-600"
                title="Runs a provider quota, a dead key, a transport failure or a harness collapse decided the outcome of. They are absent from every metric below — comparing them would compare outages, not models."
              >
                Excluded — infrastructure ⊘
              </div>
              <div className="px-4 py-2 text-center tabular-nums text-gray-500">
                {a.contaminated || 0}
              </div>
              <div className="px-4 py-2 text-center tabular-nums text-gray-500">
                {b.contaminated || 0}
              </div>
            </div>
          ) : null}
          {METRICS.map((m) => {
            const va = num(a[m.key])
            const vb = num(b[m.key])
            const result = compare(va, vb, m.direction)
            return (
              <div key={m.key as string} className="grid grid-cols-3 border-b last:border-0 text-sm">
                <div className="px-4 py-2 text-gray-600">
                  {m.label}
                  <span className="ml-2 text-xs text-gray-400">
                    ({m.direction === 'higher_better' ? 'higher better' : 'lower better'})
                  </span>
                </div>
                <div className="px-4 py-2 flex items-center justify-center gap-2 tabular-nums">
                  <Indicator
                    kind={
                      result === 'incomparable'
                        ? 'incomparable'
                        : result === 'tie'
                          ? 'tie'
                          : result === 'a'
                            ? 'winner'
                            : 'loser'
                    }
                  />
                  <span className={va === null ? 'text-gray-400' : undefined}>
                    {va === null ? '—' : m.format(va)}
                  </span>
                </div>
                <div className="px-4 py-2 flex items-center justify-center gap-2 tabular-nums">
                  <Indicator
                    kind={
                      result === 'incomparable'
                        ? 'incomparable'
                        : result === 'tie'
                          ? 'tie'
                          : result === 'b'
                            ? 'winner'
                            : 'loser'
                    }
                  />
                  <span className={vb === null ? 'text-gray-400' : undefined}>
                    {vb === null ? '—' : m.format(vb)}
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
          {data.length < 2
            ? 'Need at least two configs with data to compare'
            : 'Select Config A and Config B to compare'}
        </div>
      )}
    </div>
  )
}
