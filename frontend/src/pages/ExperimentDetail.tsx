import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import { experimentsApi } from '@/api/client'
import type { ExperimentDetail as ExperimentDetailType, ExperimentReport } from '@/types'
import { StatusPill } from './Experiments'
import { ArrowLeft, Copy, Download, Pause, Play, RotateCcw, Square } from 'lucide-react'

const CONFIG_COLORS = ['#2563eb', '#dc2626', '#16a34a', '#9333ea', '#ea580c', '#0891b2', '#ca8a04', '#db2777']

function heatStyle(mean: number | null | undefined): React.CSSProperties {
  if (mean == null) return { backgroundColor: '#f3f4f6', color: '#9ca3af' }
  const hue = Math.max(0, Math.min(120, mean * 12)) // 0 → red, 10 → green
  return { backgroundColor: `hsl(${hue}, 75%, 88%)`, color: `hsl(${hue}, 80%, 22%)` }
}

function fmt(v: number | null | undefined, digits = 2): string {
  return v == null ? '—' : v.toFixed(digits)
}

function ProgressTab({ detail, onCell }: { detail: ExperimentDetailType; onCell: (config: string, caseKey: string) => void }) {
  const cases = detail.dataset_cases
  const cells = new Map(detail.matrix.map((c) => [`${c.config_key}|${c.case_key}`, c.counts]))
  if (detail.matrix.length === 0) {
    return <div className="text-sm text-gray-500 p-4">No runs yet — the matrix materializes when the experiment starts.</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="text-sm border-separate" style={{ borderSpacing: 4 }}>
        <thead>
          <tr>
            <th className="text-left text-xs text-gray-500 px-2">config \ case</th>
            {cases.map((c) => (
              <th key={c.case_key} className="text-xs text-gray-500 font-normal px-2 max-w-32 truncate" title={c.title}>
                {c.case_key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {detail.configurations.map((cfg) => (
            <tr key={cfg.config_key}>
              <td className="text-xs text-gray-700 font-medium px-2 whitespace-nowrap" title={cfg.label}>
                {cfg.config_key} <span className="text-gray-400">{cfg.label}</span>
              </td>
              {cases.map((c) => {
                const counts = cells.get(`${cfg.config_key}|${c.case_key}`) || {}
                return (
                  <td key={c.case_key} onClick={() => onCell(cfg.config_key, c.case_key)}
                    className="border rounded-lg px-2 py-1.5 bg-white hover:bg-gray-50 cursor-pointer text-center">
                    <div className="flex items-center justify-center gap-1 text-xs">
                      {counts.success ? <span className="text-green-600 font-medium">{counts.success}✓</span> : null}
                      {counts.failed ? <span className="text-red-600 font-medium">{counts.failed}✗</span> : null}
                      {counts.running ? <span className="text-blue-600 font-medium">{counts.running}…</span> : null}
                      {counts.pending ? <span className="text-gray-400">{counts.pending}·</span> : null}
                      {counts.skipped ? <span className="text-amber-600">{counts.skipped}s</span> : null}
                      {Object.keys(counts).length === 0 && <span className="text-gray-300">—</span>}
                    </div>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-xs text-gray-400 mt-2">✓ success · ✗ failed · … running · · pending · s skipped — click a cell for run details</div>
    </div>
  )
}

function ReportTab({ id, isTerminal }: { id: string; isTerminal: boolean }) {
  const [method, setMethod] = useState<'bt' | 'elo'>('bt')
  const { data: report, isLoading } = useQuery({
    queryKey: ['experiment-report', id, method],
    queryFn: () => experimentsApi.report(id, { method }),
    refetchInterval: isTerminal ? false : 10000,
  })
  if (isLoading || !report) return <div className="text-sm text-gray-500 p-4">Assembling report…</div>
  return <ReportView report={report} method={method} setMethod={setMethod} />
}

function ReportView({ report, method, setMethod }: {
  report: ExperimentReport
  method: 'bt' | 'elo'
  setMethod: (m: 'bt' | 'elo') => void
}) {
  const colorByConfig = new Map(
    report.summary.per_config.map((c, i) => [c.config_key, CONFIG_COLORS[i % CONFIG_COLORS.length]]),
  )
  return (
    <div className="space-y-6">
      {report.partial && (
        <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
          Partial report — the experiment is still running ({report.n_terminal_runs} runs settled).
        </div>
      )}

      <section>
        <h3 className="font-semibold text-gray-900 mb-2">Summary</h3>
        <div className="bg-white border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-3 py-2">Configuration</th>
                <th className="px-3 py-2">Runs</th>
                <th className="px-3 py-2">Success</th>
                <th className="px-3 py-2">Quality</th>
                <th className="px-3 py-2">Trajectory</th>
                <th className="px-3 py-2">Cost avg</th>
                <th className="px-3 py-2">Time avg</th>
              </tr>
            </thead>
            <tbody>
              {report.summary.per_config.map((c) => (
                <tr key={c.config_key} className="border-t">
                  <td className="px-3 py-2 font-medium" style={{ color: colorByConfig.get(c.config_key) }}>
                    {c.config_key} <span className="text-gray-500 font-normal">{c.label}</span>
                  </td>
                  <td className="px-3 py-2">{c.n_runs}</td>
                  <td className="px-3 py-2">{c.success_rate != null ? `${(c.success_rate * 100).toFixed(0)}%` : '—'}</td>
                  <td className="px-3 py-2">{fmt(c.quality_mean)}</td>
                  <td className="px-3 py-2">{fmt(c.trajectory_mean)}</td>
                  <td className="px-3 py-2">${fmt(c.cost_mean, 3)}</td>
                  <td className="px-3 py-2">{c.duration_mean != null ? `${Math.round(c.duration_mean)}s` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h3 className="font-semibold text-gray-900 mb-2">Quality profile heatmap</h3>
        {report.heatmap.dimensions.length === 0 ? (
          <p className="text-sm text-gray-500">No rubric dimension scores yet (configure a judge model to score runs).</p>
        ) : (
          <div className="bg-white border rounded-lg overflow-x-auto p-3">
            <table className="text-sm border-separate" style={{ borderSpacing: 3 }}>
              <thead>
                <tr>
                  <th className="text-left text-xs text-gray-500 px-2">config</th>
                  {report.heatmap.dimensions.map((d) => (
                    <th key={d} className="text-xs text-gray-500 font-normal px-2">{d}</th>
                  ))}
                  <th className="text-xs text-gray-700 font-medium px-2">weighted</th>
                </tr>
              </thead>
              <tbody>
                {report.heatmap.rows.map((row) => (
                  <tr key={row.config_key}>
                    <td className="text-xs font-medium px-2 whitespace-nowrap" title={row.label}>{row.config_key}</td>
                    {report.heatmap.dimensions.map((d) => {
                      const cell = row.cells[d]
                      return (
                        <td key={d} className="rounded px-3 py-2 text-center text-sm font-medium" style={heatStyle(cell?.mean)}
                          title={cell ? `n=${cell.n}${cell.std != null ? ` · std=${cell.std}` : ''}` : ''}>
                          {fmt(cell?.mean, 1)}
                        </td>
                      )
                    })}
                    <td className="rounded px-3 py-2 text-center text-sm font-bold" style={heatStyle(row.weighted_score.mean)}>
                      {fmt(row.weighted_score.mean, 1)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">Pareto frontier <span className="text-xs text-gray-400 font-normal">quality × cost (size = time)</span></h3>
          <div className="bg-white border rounded-lg p-3 h-72">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 10, right: 20, bottom: 10, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis type="number" dataKey="cost" name="cost" unit="$" tick={{ fontSize: 11 }} />
                <YAxis type="number" dataKey="quality" name="quality" domain={[0, 10]} tick={{ fontSize: 11 }} />
                <ZAxis type="number" dataKey="time" range={[60, 400]} name="time" unit="s" />
                <Tooltip cursor={{ strokeDasharray: '3 3' }}
                  formatter={(v) => (typeof v === 'number' ? v.toFixed(3) : String(v ?? ''))}
                  labelFormatter={() => ''} />
                <Legend />
                <Scatter name="frontier" data={report.pareto.points.filter((p) => p.on_frontier)} fill="#16a34a" />
                <Scatter name="dominated" data={report.pareto.points.filter((p) => !p.on_frontier)} fill="#9ca3af" />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section>
          <h3 className="font-semibold text-gray-900 mb-2">Outcome × Trajectory <span className="text-xs text-gray-400 font-normal">per run</span></h3>
          <div className="bg-white border rounded-lg p-3 h-72">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 10, right: 20, bottom: 10, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis type="number" dataKey="outcome" name="outcome" domain={[0, 10]} tick={{ fontSize: 11 }} />
                <YAxis type="number" dataKey="trajectory" name="trajectory" domain={[0, 10]} tick={{ fontSize: 11 }} />
                <Tooltip cursor={{ strokeDasharray: '3 3' }} />
                <Legend />
                {report.summary.per_config.map((c) => (
                  <Scatter key={c.config_key} name={c.config_key}
                    data={report.scatter.filter((p) => p.config_key === c.config_key && p.outcome != null && p.trajectory != null)}
                    fill={colorByConfig.get(c.config_key)} />
                ))}
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>

      <section>
        <div className="flex items-center gap-3 mb-2">
          <h3 className="font-semibold text-gray-900">Pairwise leaderboard</h3>
          <div className="flex rounded-lg border overflow-hidden text-xs">
            {(['bt', 'elo'] as const).map((m) => (
              <button key={m} onClick={() => setMethod(m)}
                className={`px-2.5 py-1 ${method === m ? 'bg-blue-600 text-white' : 'bg-white hover:bg-gray-50'}`}>
                {m === 'bt' ? 'Bradley-Terry' : 'Elo'}
              </button>
            ))}
          </div>
          <span className="text-xs text-gray-400">derived from pointwise scores, case-paired</span>
        </div>
        {report.leaderboard.status !== 'ok' ? (
          <p className="text-sm text-gray-500">Not enough scored runs for a leaderboard ({report.leaderboard.status}).</p>
        ) : (
          <div className="bg-white border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2">#</th>
                  <th className="px-3 py-2">Configuration</th>
                  <th className="px-3 py-2">Rating</th>
                  <th className="px-3 py-2">95% CI</th>
                  <th className="px-3 py-2">W / L / T</th>
                </tr>
              </thead>
              <tbody>
                {report.leaderboard.players.map((p) => (
                  <tr key={p.player} className="border-t">
                    <td className="px-3 py-2 font-bold">{p.rank}</td>
                    <td className="px-3 py-2 font-medium">{p.player} <span className="text-gray-500 font-normal">{p.label}</span></td>
                    <td className="px-3 py-2">{p.rating.toFixed(0)}</td>
                    <td className="px-3 py-2 text-gray-500">
                      {p.ci_low != null ? `${p.ci_low.toFixed(0)} – ${p.ci_high?.toFixed(0)}` : '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-600">{p.wins ?? 0} / {p.losses ?? 0} / {p.ties ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h3 className="font-semibold text-gray-900 mb-2">Statistical significance <span className="text-xs text-gray-400 font-normal">Welch t-test (primary) + Mann-Whitney U (approx); ★ = p &lt; 0.05</span></h3>
        {report.significance.length === 0 ? (
          <p className="text-sm text-gray-500">Not enough samples per cell yet (need n ≥ 3 scored runs on both sides).</p>
        ) : (
          <div className="bg-white border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2">Pair</th>
                  <th className="px-3 py-2">Metric</th>
                  <th className="px-3 py-2">Welch p</th>
                  <th className="px-3 py-2">Mann-Whitney p</th>
                  <th className="px-3 py-2">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {report.significance.map((s) => (
                  <tr key={`${s.a}-${s.b}-${s.metric}`} className="border-t">
                    <td className="px-3 py-2">{s.a} vs {s.b}</td>
                    <td className="px-3 py-2 text-gray-600">{s.metric}</td>
                    <td className="px-3 py-2">{s.welch ? s.welch.p.toFixed(4) : '—'}</td>
                    <td className="px-3 py-2">{s.mann_whitney ? s.mann_whitney.p.toFixed(4) : '—'}</td>
                    <td className="px-3 py-2">
                      {s.significant
                        ? <span className="text-green-700 font-medium">★ significant</span>
                        : <span className="text-gray-400">not significant</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">Failure modes</h3>
          <div className="bg-white border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2">Configuration</th>
                  <th className="px-3 py-2">Statuses</th>
                  <th className="px-3 py-2">Failure classes</th>
                </tr>
              </thead>
              <tbody>
                {report.failure_modes.per_config.map((f) => (
                  <tr key={f.config_key} className="border-t align-top">
                    <td className="px-3 py-2 font-medium">{f.config_key}</td>
                    <td className="px-3 py-2 text-gray-600">
                      {Object.entries(f.statuses).map(([s, n]) => `${s}: ${n}`).join(' · ') || '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-600">
                      {Object.keys(f.classes).length
                        ? Object.entries(f.classes).map(([c, n]) => `${c}: ${n}`).join(' · ')
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section>
          <h3 className="font-semibold text-gray-900 mb-2">Orchestrator on / off</h3>
          {!report.orchestrator.on || !report.orchestrator.off ? (
            <p className="text-sm text-gray-500">
              Add configurations on both sides of the <code>orchestrator</code> axis to compare orchestration impact.
            </p>
          ) : (
            <div className="bg-white border rounded-lg p-4">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-gray-500 uppercase">
                  <tr><th /><th className="py-1">orchestrator: on</th><th className="py-1">off</th><th className="py-1">Δ (on − off)</th></tr>
                </thead>
                <tbody>
                  {([
                    ['quality_mean', 'Quality'],
                    ['trajectory_mean', 'Trajectory'],
                    ['success_rate', 'Success rate'],
                    ['cost_mean', 'Cost avg, $'],
                    ['duration_mean', 'Time avg, s'],
                  ] as const).map(([key, label]) => (
                    <tr key={key} className="border-t">
                      <td className="py-1.5 text-gray-600">{label}</td>
                      <td className="py-1.5">{fmt(report.orchestrator.on?.[key])}</td>
                      <td className="py-1.5">{fmt(report.orchestrator.off?.[key])}</td>
                      <td className="py-1.5 font-medium">{fmt(report.orchestrator.delta?.[key])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-xs text-gray-400 mt-2">
                on: {report.orchestrator.on.configs.join(', ')} · off: {report.orchestrator.off.configs.join(', ')}
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function RunsTab({ id, detail, filter }: {
  id: string
  detail: ExperimentDetailType
  filter: { config?: string; case?: string }
}) {
  const [config, setConfig] = useState(filter.config || '')
  const [caseKey, setCaseKey] = useState(filter.case || '')
  const { data: rows = [] } = useQuery({
    queryKey: ['experiment-results', id, config, caseKey],
    queryFn: () =>
      experimentsApi.results(id, {
        ...(config ? { config } : {}),
        ...(caseKey ? { case: caseKey } : {}),
      }),
  })
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <select value={config} onChange={(e) => setConfig(e.target.value)}
          className="px-2 py-1.5 border rounded text-sm bg-white">
          <option value="">all configurations</option>
          {detail.configurations.map((c) => (
            <option key={c.config_key} value={c.config_key}>{c.config_key} — {c.label}</option>
          ))}
        </select>
        <select value={caseKey} onChange={(e) => setCaseKey(e.target.value)}
          className="px-2 py-1.5 border rounded text-sm bg-white">
          <option value="">all cases</option>
          {detail.dataset_cases.map((c) => (
            <option key={c.case_key} value={c.case_key}>{c.case_key}</option>
          ))}
        </select>
        <span className="text-xs text-gray-400">{rows.length} runs</span>
      </div>
      <div className="bg-white border rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
            <tr>
              <th className="px-3 py-2">Cell</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Quality</th>
              <th className="px-3 py-2">Trajectory</th>
              <th className="px-3 py-2">Cost</th>
              <th className="px-3 py-2">Time</th>
              <th className="px-3 py-2">Result</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.config_key}-${r.case_key}-${r.run_index}`} className="border-t">
                <td className="px-3 py-2 whitespace-nowrap text-gray-700">
                  {r.config_key} · {r.case_key} · #{r.run_index + 1}
                </td>
                <td className="px-3 py-2">
                  <span className={
                    r.status === 'success' ? 'text-green-600' :
                    r.status === 'failed' ? 'text-red-600' :
                    r.status === 'running' ? 'text-blue-600' : 'text-gray-400'
                  }>{r.status}</span>
                </td>
                <td className="px-3 py-2">{fmt(r.weighted_score, 1)}</td>
                <td className="px-3 py-2">{fmt(r.trajectory_score, 1)}</td>
                <td className="px-3 py-2">${r.cost_usd.toFixed(3)}</td>
                <td className="px-3 py-2">{r.duration_seconds != null ? `${r.duration_seconds}s` : '—'}</td>
                <td className="px-3 py-2 text-gray-500 max-w-md truncate" title={r.result_summary || ''}>
                  {r.result_summary || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function ExperimentDetail() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<'progress' | 'report' | 'runs'>('progress')
  const [runsFilter, setRunsFilter] = useState<{ config?: string; case?: string }>({})

  const { data: detail } = useQuery({
    queryKey: ['experiment', id],
    queryFn: () => experimentsApi.get(id),
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 4000 : false),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['experiment', id] })
    queryClient.invalidateQueries({ queryKey: ['experiments'] })
  }
  const runMutation = useMutation({ mutationFn: () => experimentsApi.run(id), onSuccess: invalidate })
  const pauseMutation = useMutation({ mutationFn: () => experimentsApi.pause(id), onSuccess: invalidate })
  const resumeMutation = useMutation({ mutationFn: () => experimentsApi.resume(id), onSuccess: invalidate })
  const cancelMutation = useMutation({ mutationFn: () => experimentsApi.cancel(id), onSuccess: invalidate })

  const cloneMutation = useMutation({
    mutationFn: async (alsoRun: boolean) => {
      const clone = await experimentsApi.clone(id, {})
      if (alsoRun) await experimentsApi.run(clone.id)
      return clone
    },
    onSuccess: (clone) => {
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
      navigate(`/experiments/${clone.id}`)
    },
  })

  const download = async (format: 'csv' | 'json') => {
    const blob = await experimentsApi.export(id, format)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `experiment-${id}.${format}`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (!detail) return <div className="p-6 text-sm text-gray-500">Loading…</div>

  const isTerminal = ['completed', 'capped', 'failed', 'cancelled'].includes(detail.status)

  return (
    <div className="p-6">
      <button onClick={() => navigate('/experiments')}
        className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700 mb-3">
        <ArrowLeft className="h-4 w-4" /> Experiments
      </button>

      <div className="flex items-start justify-between mb-1">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-3">
            {detail.name} <StatusPill status={detail.status} />
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            {detail.n_configs} configs × {detail.n_cases} cases × {detail.n_runs_per_cell} runs = {detail.total_runs} ·
            spent ${detail.accumulated_cost_usd.toFixed(2)}
            {detail.budget_limit_usd != null && ` / $${detail.budget_limit_usd.toFixed(2)}`}
            {detail.description ? ` · ${detail.description}` : ''}
          </p>
        </div>
        <div className="flex gap-2">
          {detail.status === 'draft' && (
            <button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
              <Play className="h-4 w-4" /> Run
            </button>
          )}
          {detail.status === 'running' && (
            <button onClick={() => pauseMutation.mutate()}
              className="flex items-center gap-1.5 px-3 py-2 border rounded-lg hover:bg-gray-50 text-sm">
              <Pause className="h-4 w-4" /> Pause
            </button>
          )}
          {detail.status === 'paused' && (
            <button onClick={() => resumeMutation.mutate()}
              className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
              <Play className="h-4 w-4" /> Resume
            </button>
          )}
          {!isTerminal && detail.status !== 'draft' && (
            <button onClick={() => { if (confirm('Cancel this experiment? Partial results are kept.')) cancelMutation.mutate() }}
              className="flex items-center gap-1.5 px-3 py-2 border border-red-200 text-red-600 rounded-lg hover:bg-red-50 text-sm">
              <Square className="h-4 w-4" /> Cancel
            </button>
          )}
          <button onClick={() => cloneMutation.mutate(false)} title="Clone as a new draft (edit before running)"
            className="flex items-center gap-1.5 px-3 py-2 border rounded-lg hover:bg-gray-50 text-sm">
            <Copy className="h-4 w-4" /> Clone
          </button>
          {isTerminal && (
            <button onClick={() => cloneMutation.mutate(true)} title="Full reproduction: clone + run"
              className="flex items-center gap-1.5 px-3 py-2 border rounded-lg hover:bg-gray-50 text-sm">
              <RotateCcw className="h-4 w-4" /> Re-run
            </button>
          )}
          <button onClick={() => download('csv')} title="Export runs as CSV"
            className="flex items-center gap-1.5 px-3 py-2 border rounded-lg hover:bg-gray-50 text-sm">
            <Download className="h-4 w-4" /> CSV
          </button>
          <button onClick={() => download('json')} title="Export runs as JSON"
            className="flex items-center gap-1.5 px-3 py-2 border rounded-lg hover:bg-gray-50 text-sm">
            <Download className="h-4 w-4" /> JSON
          </button>
        </div>
      </div>
      {detail.error && <div className="text-xs text-red-600 mb-2">{detail.error}</div>}

      <div className="flex gap-1 border-b mb-4 mt-4">
        {(['progress', 'report', 'runs'] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
              tab === t ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}>
            {t === 'progress' ? 'Progress' : t === 'report' ? 'Report' : 'Runs'}
          </button>
        ))}
      </div>

      {tab === 'progress' && (
        <ProgressTab detail={detail} onCell={(config, caseKey) => { setRunsFilter({ config, case: caseKey }); setTab('runs') }} />
      )}
      {tab === 'report' && <ReportTab id={id} isTerminal={isTerminal} />}
      {tab === 'runs' && <RunsTab id={id} detail={detail} filter={runsFilter} />}
    </div>
  )
}
