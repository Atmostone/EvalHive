import { Fragment, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import { experimentsApi, qualityApi } from '@/api/client'
import RunAnalysis from '@/components/quality/RunAnalysis'
import SummaryRadarPanel from '@/components/quality/SummaryRadarPanel'
import type { ExperimentConfig, ExperimentDetail as ExperimentDetailType, ExperimentReport, ExperimentTrustAxis } from '@/types'
import { StatusPill } from './Experiments'
import { ArrowLeft, Copy, Download, Pause, Play, RefreshCw, RotateCcw, Square, Trash2, X } from 'lucide-react'

const CONFIG_COLORS = ['#2563eb', '#dc2626', '#16a34a', '#9333ea', '#ea580c', '#0891b2', '#ca8a04', '#db2777']

function heatStyle(mean: number | null | undefined): React.CSSProperties {
  if (mean == null) return { backgroundColor: '#f3f4f6', color: '#9ca3af' }
  const hue = Math.max(0, Math.min(120, mean * 12)) // 0 → red, 10 → green
  // 85% sat / 85% light — same stronger tint as cellHeat so the Report heatmaps
  // stay legible under red-green colour-blindness; the printed number is the cue.
  return { backgroundColor: `hsl(${hue}, 85%, 85%)`, color: `hsl(${hue}, 80%, 22%)` }
}

function fmt(v: number | null | undefined, digits = 2): string {
  return v == null ? '—' : v.toFixed(digits)
}

type HeatMode = 'quality' | 'trajectory' | 'human' | 'off'

// Plain-language names + one-line explanations for the Heat toggle. The internal
// E-codes are kept (the team uses them) but always paired with what they MEAN, so a
// non-author is not left guessing what "q" / "t" measure.
const HEAT_LABEL: Record<HeatMode, string> = {
  quality: 'Outcome quality',
  trajectory: 'Process trajectory',
  human: 'Human',
  off: 'off',
}
const HEAT_HELP: Record<HeatMode, string> = {
  quality:
    'Outcome quality — the LLM judge rubric score of the final RESULT. Red = weak result, green = strong; higher is better.',
  trajectory:
    'Process trajectory — the 6-axis judge score of HOW the agent worked: efficiency, tool choice, error recovery, goal alignment… Higher = cleaner process.',
  human:
    'Human — your own dimension ratings and approve/reject verdict on the run; the ground-truth oracle used for judge calibration.',
  off: 'No cell colouring — show only the run-outcome glyphs.',
}

// Significance-table metric keys are programmatic (weighted_score / trajectory_score
// / dim:<x>); map them to human names + which judge produced them, so a reader can
// tell outcome (E-02) rows from process (E-07) rows at a glance.
function metricLabel(metric: string): string {
  if (metric === 'weighted_score') return 'Overall quality'
  if (metric === 'trajectory_score') return 'Overall trajectory'
  if (metric.startsWith('dim:')) return metric.slice(4).replace(/_/g, ' ')
  return metric.replace(/_/g, ' ')
}
function metricJudge(metric: string): { label: string; cls: string } {
  if (metric === 'trajectory_score')
    return { label: 'Trajectory', cls: 'text-purple-700 bg-purple-50' }
  // weighted_score + every dim:* are outcome-rubric metrics from the E-02 judge.
  return { label: 'Quality', cls: 'text-blue-700 bg-blue-50' }
}

// --- SPA-76 reliability gate ------------------------------------------------
// Per-axis trustworthiness of the E-07 process judge, from REAL calibration only
// (judge↔human κ, or the loop anchor). Surfaced as a small badge that quarantines
// below-threshold axes so an unreliable axis can't silently imply a process "win".
type AxisReliability = NonNullable<ExperimentReport['axis_reliability']>['axes'][string]
type ReliabilityStatus = AxisReliability['status']

// SPA-88: six statuses, four colours. The taxonomy underneath is richer than the
// light on top — 'insufficient' and 'not_calibrated' are both grey because both
// mean "we do not know", and 'rank_only' shares amber with 'moderate_agreement'
// because both mean "usable, with a caveat" — but the caveats differ, so the word
// and the tooltip differ.
const RELIABILITY_META: Record<ReliabilityStatus, { glyph: string; cls: string; word: string }> = {
  reliable_absolute: { glyph: '✓', cls: 'text-green-700', word: 'reliable' },
  moderate_agreement: { glyph: '~', cls: 'text-amber-600', word: 'moderate agreement' },
  rank_only: { glyph: '~', cls: 'text-amber-600', word: 'rank only' },
  insufficient: { glyph: 'n/a', cls: 'text-gray-400', word: 'insufficient data' },
  unreliable: { glyph: '⚠', cls: 'text-red-600', word: 'unreliable' },
  not_calibrated: { glyph: 'n/a', cls: 'text-gray-400', word: 'not calibrated' },
}


// Failure types the report excludes from its aggregates (SPA-87), in the words an
// operator uses for them. An unknown key falls through to the raw type rather
// than being hidden — a run dropped for a reason the UI cannot name is worse than
// an ugly label.
const EXCLUSION_LABELS: Record<string, string> = {
  llm_rate_limit: 'provider rate limit / quota',
  llm_auth: 'provider auth — dead key or spent credit',
  llm_transient: 'provider timeout or 5xx',
  infra: 'harness failure',
}

function reliabilitySource(source?: string): string {
  if (source === 'human') return 'a human rater'
  if (source === 'structural') return 'the deterministic loop counter'
  return 'no reference'
}

function reliabilityTooltip(a?: AxisReliability): string {
  if (!a) return ''
  if (a.status === 'not_calibrated')
    return 'Reliability: not calibrated — no human rating or structural anchor for this axis. The judge score is shown but unverified, and the trusted view leaves it out.'
  const k = a.kappa != null ? `κ ${a.kappa.toFixed(2)}` : 'κ undefined'
  const kci = a.kappa_ci
    ? ` [95% CI ${a.kappa_ci.lo.toFixed(2)}–${a.kappa_ci.hi.toFixed(2)}]`
    : ''
  const r = a.rho != null ? ` · rank ρ ${a.rho.toFixed(2)}` : ''
  // What this axis is allowed to DRIVE — the point of the taxonomy, and the thing
  // a reader actually needs from the badge.
  const licence =
    a.status === 'rank_only'
      ? ' Scale-shifted judge: ranks agree with the human, absolute scores do not. It may carry a rank test on its own scores and nothing else — no mean, no frontier, no leaderboard, since all three are built from magnitudes it cannot support.'
      : a.status === 'insufficient'
        ? ' Too few rated pairs to say anything either way — excluded from the trusted view as unknown, not as bad.'
        : a.status === 'unreliable'
          ? ' Quarantined: it drives no aggregate, no Pareto point and no significant row in the trusted view.'
          : ' Cleared for numeric aggregates in the trusted view.'
  return `Reliability: ${RELIABILITY_META[a.status].word} — judge vs ${reliabilitySource(a.source)} (${k}${kci}${r}, n=${a.n}). Bar: κ≥0.6 reliable · 0.4–0.6 moderate · <0.4 rank-only if ρ≥0.5, else unreliable.${licence}`
}

// SPA-84: an experiment id alone no longer identifies a fixed set of runs — every
// mutation (retry, add-config, retire-config) bumps the revision. Showing it makes
// "which version of this experiment am I looking at" answerable from the page.
// SPA-84: what a configuration actually resolved to when the experiment started.
// The column header is the only place a config is named, so it is where the frozen
// resolution belongs — otherwise "cfg-01" says nothing about which model ran.
function configTooltip(cfg: ExperimentConfig): string {
  const r = cfg.resolved
  if (!r) return cfg.config_key
  const parts = [cfg.config_key]
  if (r.model_api_name) parts.push(`model ${r.model_api_name}`)
  if (r.provider_name) parts.push(`via ${r.provider_name}`)
  if (r.template_name) parts.push(`template ${r.template_name}`)
  if (r.template_content_sha256) parts.push(`template hash ${r.template_content_sha256}`)
  if (r.resolved_at) parts.push(`frozen ${new Date(r.resolved_at).toLocaleString()}`)
  return parts.join(' · ')
}

function RevisionBadge({ revision, retired }: { revision: number; retired: number }) {
  if (!revision) return null
  const mutated = revision > 1
  return (
    <span
      className={`text-xs px-2 py-0.5 rounded-full font-medium ${
        mutated ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500'
      }`}
      title={
        mutated
          ? `Revision ${revision} — this experiment has been mutated ${revision - 1} time(s) since it was created (retry, add-config or retire-config). Superseded runs are kept in the attempt ledger.${retired ? ` ${retired} configuration(s) retired.` : ''}`
          : 'Revision 1 — unchanged since it was created.'
      }
    >
      rev {revision}
      {retired > 0 && ` · ${retired} retired`}
    </span>
  )
}

function ReliabilityBadge({ a }: { a?: AxisReliability }) {
  if (!a) return null
  const m = RELIABILITY_META[a.status]
  return (
    <span className={`ml-1 text-[10px] font-semibold ${m.cls}`} title={reliabilityTooltip(a)}>
      {m.glyph}
    </span>
  )
}

// The overall trajectory metric is an aggregate of the 6 axes — only as reliable as
// its weakest calibrated axis. Worst-case status drives the warning (honest: an
// aggregate that folds in an axis the judge gets wrong is itself suspect).
function trajectoryAggregateStatus(report: ExperimentReport): ReliabilityStatus | null {
  const axes = report.axis_reliability?.axes
  if (!axes) return null
  const sourced = Object.values(axes).filter((v) => v.source !== 'none')
  if (sourced.length === 0) return 'not_calibrated'
  if (sourced.some((v) => v.status === 'unreliable')) return 'unreliable'
  if (sourced.some((v) => v.status === 'insufficient')) return 'insufficient'
  if (sourced.some((v) => v.status === 'rank_only')) return 'rank_only'
  if (sourced.some((v) => v.status === 'moderate_agreement')) return 'moderate_agreement'
  return 'reliable_absolute'
}

// Per-cell dimension/axis means (sorted worst-first by the backend) → a compact
// "low→high" line for the cell tooltip, so a reader can see which axis drags the
// score down without opening the run. (SPA-73)
function fmtBreakdown(rows?: { name: string; mean: number }[]): string {
  if (!rows || rows.length === 0) return ''
  return rows.map((r) => `${r.name} ${r.mean}`).join(' · ')
}

// Compact token count (effort metric, SPA-77): 1.67M / 760k / 540.
function fmtTokens(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`
  return `${Math.round(v)}`
}

// SPA-77: difficulty-normalized relative effort (×median). 1.0 = typical effort
// for the cases a config ran; >1 heavier (amber), <1 lighter (green).
function relEffortStyle(v: number | null | undefined): string {
  if (v == null) return 'text-gray-400'
  if (v > 1.15) return 'text-amber-700'
  if (v < 0.85) return 'text-green-700'
  return 'text-gray-600'
}

// Subtle red→green cell tint (0 → red, 10 → green) so it never overpowers the
// status glyphs printed on top of it. 85% sat / 85% light keeps the red↔green
// signal distinguishable under deuteranopia; the numeric q/t/human score printed in
// the cell stays the primary cue, colour is only an accent.
function cellHeat(mean: number | null | undefined): React.CSSProperties {
  // No score for the selected heat (run failed, or the judge produced no score):
  // a subtle diagonal hatch reads as "no data" instead of a blank white cell that
  // looks like a rendering bug.
  if (mean == null) {
    return { backgroundImage: 'repeating-linear-gradient(45deg, #eceef1 0, #eceef1 3px, transparent 3px, transparent 7px)' }
  }
  const hue = Math.max(0, Math.min(120, mean * 12))
  return { backgroundColor: `hsl(${hue}, 85%, 85%)` }
}

function CloneModal({ detail, pending, onClose, onClone }: {
  detail: ExperimentDetailType
  pending: boolean
  onClose: () => void
  onClone: (opts: { name?: string; changes?: Record<string, unknown> }) => void
}) {
  const [name, setName] = useState(`${detail.name} (copy)`)
  const [nRuns, setNRuns] = useState(String(detail.n_runs_per_cell))
  const [budget, setBudget] = useState(detail.budget_limit_usd != null ? String(detail.budget_limit_usd) : '')
  const submit = () => {
    const changes: Record<string, unknown> = {}
    if (Number(nRuns) !== detail.n_runs_per_cell) changes.n_runs_per_cell = Number(nRuns)
    const b = budget === '' ? null : Number(budget)
    if (b !== (detail.budget_limit_usd ?? null)) changes.budget_limit_usd = b
    onClone({ name: name.trim() || undefined, changes })
  }
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl w-full max-w-md p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold">Clone experiment</h2>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-100"><X className="h-5 w-5" /></button>
        </div>
        <p className="text-xs text-gray-500 mb-3">
          New draft with the same frozen dataset &amp; configuration matrix. Tweak name / runs / budget; everything else is copied.
        </p>
        <div className="space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Runs per cell (N)</label>
              <input type="number" min={1} max={20} value={nRuns} onChange={(e) => setNRuns(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Budget (USD)</label>
              <input type="number" step="0.01" min={0} value={budget} placeholder="no limit" onChange={(e) => setBudget(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <button onClick={submit} disabled={pending}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium">
            {pending ? 'Cloning…' : 'Create clone (draft)'}
          </button>
        </div>
      </div>
    </div>
  )
}

function ProgressTab({ detail, onCell }: { detail: ExperimentDetailType; onCell: (config: string, caseKey: string) => void }) {
  const queryClient = useQueryClient()
  const retireMutation = useMutation({
    mutationFn: (configKey: string) => experimentsApi.retireConfig(detail.id, configKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['experiment', detail.id] }),
  })
  // Verifiable bench: an executable checker provides a ground-truth verdict, but
  // the checker is itself unreliable (~21% vs gold), so the LLM judge (E-02) and
  // human (E-05) are shown ALONGSIDE it as independent oracles — all three are
  // available as heat views and on every cell (triangulation). Default to the
  // trajectory view on verifiable benches where the cell already shows ✔checker.
  const verifiable = detail.matrix.some((c) => (c.external_total ?? 0) > 0)
  const anyHuman = detail.matrix.some((c) => (c.human_rated ?? 0) > 0)
  const [heat, setHeat] = useState<HeatMode>(verifiable ? 'trajectory' : 'quality')
  const cases = detail.dataset_cases
  const cells = new Map(detail.matrix.map((c) => [`${c.config_key}|${c.case_key}`, c]))
  // SPA-84: a retired configuration keeps its entry (and its lineage) but its
  // cells are out of the matrix. Drawing its column anyway produced a phantom
  // strip of "—" and made the header count disagree with what was on screen.
  const liveConfigs = detail.configurations.filter((c) => !c.retired_at)
  const retiredConfigs = detail.configurations.filter((c) => c.retired_at)
  // The backend refuses to retire while running, and refuses the last live one.
  const canRetire = detail.status !== 'running' && detail.status !== 'draft' && liveConfigs.length > 1
  if (detail.matrix.length === 0) {
    return (
      <div className="text-sm text-gray-500 p-4 max-w-2xl space-y-1">
        <p className="font-medium text-gray-700">No runs yet.</p>
        <p>
          The matrix materializes when the experiment starts — one row per dataset case, one column per
          configuration, each cell holding the N runs of that case under that config.
        </p>
        <p className="text-gray-400">
          {detail.status === 'draft'
            ? 'This experiment is a draft — press Run (top right) to launch it.'
            : 'Cells will fill in as runs are scheduled and scored.'}
        </p>
      </div>
    )
  }
  return (
    <div className="overflow-x-auto">
      <div className="flex items-center gap-2 mb-1 text-xs">
        <span className="text-gray-500" title="Colour the matrix cells by a chosen score — red = low, green = high">Heat:</span>
        <div className="flex border rounded-lg overflow-hidden">
          {(['quality', 'trajectory', ...(anyHuman ? ['human'] : []), 'off'] as HeatMode[]).map((m) => (
            <button key={m} onClick={() => setHeat(m)} title={HEAT_HELP[m]}
              className={`px-2.5 py-1 ${heat === m ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              {HEAT_LABEL[m]}
            </button>
          ))}
        </div>
      </div>
      <p className="text-[11px] text-gray-400 mb-3 max-w-3xl">
        {HEAT_HELP[heat]}
        {liveConfigs.length > 4 && (
          <span className="text-gray-400"> · {liveConfigs.length} configs — scroll horizontally to see them all →</span>
        )}
      </p>
      <table className="text-sm border-separate w-full" style={{ borderSpacing: 4 }}>
        <thead>
          <tr>
            <th className="text-left text-xs text-gray-500 px-2 sticky top-0 left-0 bg-white z-20">case \ config</th>
            {liveConfigs.map((cfg) => (
              <th key={cfg.config_key} className="text-xs text-gray-500 font-normal px-2 whitespace-nowrap sticky top-0 bg-white z-10"
                title={configTooltip(cfg)}>
                {cfg.label}
                {/* SPA-84: retiring is only legal off the running path, and never
                    on the last live configuration. */}
                {canRetire && (
                  <button
                    onClick={() => {
                      if (confirm(`Retire "${cfg.label}"? Its ${detail.n_cases * detail.n_runs_per_cell} cell(s) keep their lineage but leave the matrix and the report. This bumps the experiment revision and cannot be undone from the UI.`)) {
                        retireMutation.mutate(cfg.config_key)
                      }
                    }}
                    disabled={retireMutation.isPending}
                    title="Retire this configuration — its runs are kept and stay reachable under Runs → include retired, but it leaves the matrix and the report."
                    className="ml-1 text-gray-300 hover:text-red-500 disabled:opacity-40"
                  >
                    ⊘
                  </button>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cases.map((c) => (
            <tr key={c.case_key}>
              <td className="text-xs text-gray-700 font-medium px-2 max-w-[16rem] truncate sticky left-0 z-10 bg-white" title={c.title}>
                {c.case_key}
              </td>
              {liveConfigs.map((cfg) => {
                const cell = cells.get(`${cfg.config_key}|${c.case_key}`)
                const counts = cell?.counts || {}
                const heatVal = heat === 'quality' ? cell?.quality_mean : heat === 'trajectory' ? cell?.trajectory_mean : heat === 'human' ? cell?.human_mean : null
                return (
                  <td key={cfg.config_key} onClick={() => onCell(cfg.config_key, c.case_key)}
                    style={heat === 'off' ? undefined : cellHeat(heatVal)}
                    title={heat !== 'off' && heatVal == null && (counts.success || counts.failed) ? `no ${heat} score for this cell — run failed or the judge produced no score (hatched = no data)` : undefined}
                    className="border rounded-lg px-2 py-1.5 hover:brightness-95 cursor-pointer text-center">
                    {/* 🔩 mechanical row: run outcome + executable checker verdict */}
                    <div className="flex items-center justify-center gap-1 text-xs">
                      <span title="run outcome + executable checker">🔩</span>
                      {counts.success ? <span className="text-green-600 font-medium">{counts.success}✓</span> : null}
                      {counts.failed ? <span className="text-red-600 font-medium">{counts.failed}✗</span> : null}
                      {counts.preprocessing ? <span className="text-purple-600 font-medium" title="preprocessing (Toolathlon seed)">{counts.preprocessing}⚙</span> : null}
                      {counts.running ? <span className="text-blue-600 font-medium">{counts.running}…</span> : null}
                      {counts.evaluating ? <span className="text-indigo-600 font-medium" title="evaluating (executable checker)">{counts.evaluating}⏳</span> : null}
                      {counts.pending ? <span className="text-gray-400">{counts.pending}·</span> : null}
                      {counts.skipped ? <span className="text-amber-600">{counts.skipped}s</span> : null}
                      {cell?.contaminated ? (
                        <span
                          className="text-gray-500"
                          title="Infrastructure decided this run's outcome — a provider quota, a dead key, a transport failure or a harness collapse. The run is still here, but its scores are excluded from every average, so this cell may show no quality at all."
                        >
                          {cell.contaminated}⊘
                        </span>
                      ) : null}
                      {Object.keys(counts).length === 0 && <span className="text-gray-300">—</span>}
                      {cell?.external_total ? (
                        <span className={cell.external_pass === cell.external_total ? 'text-green-600' : cell.external_pass ? 'text-amber-600' : 'text-red-600'}
                          title="executable verdict — passed / evaluated (Toolathlon checker)">
                          ✔{cell.external_pass}/{cell.external_total}
                        </span>
                      ) : null}
                    </div>
                    {/* ⚖️ judge row: quality (E-02) + trajectory (E-07), always shown */}
                    {(cell?.quality_mean != null || cell?.trajectory_mean != null) && (
                      <div className="text-[10px] mt-0.5 text-gray-600 tabular-nums">
                        <span title="LLM judge — q: outcome quality · t: process trajectory">⚖️</span>
                        {cell?.quality_mean != null && (
                          <span className="ml-0.5"
                            title={`outcome quality — rubric score of the result (the outcome judge)${cell.quality_std != null ? ` · σ ${cell.quality_std} across runs` : ''}${fmtBreakdown(cell.dim_means) ? `\nby dimension (low→high): ${fmtBreakdown(cell.dim_means)}` : ''}`}>
                            q{cell.quality_mean}{cell.quality_std != null && <span className="text-gray-400">±{cell.quality_std}</span>}
                          </span>
                        )}
                        {cell?.trajectory_mean != null && (
                          <span className="ml-1"
                            title={`process trajectory — 6-axis score of how the agent worked (the trajectory judge)${cell.trajectory_std != null ? ` · σ ${cell.trajectory_std} across runs` : ''}${fmtBreakdown(cell.axis_means) ? `\nby axis (low→high): ${fmtBreakdown(cell.axis_means)}` : ''}`}>
                            t{cell.trajectory_mean}{cell.trajectory_std != null && <span className="text-gray-400">±{cell.trajectory_std}</span>}
                          </span>
                        )}
                      </div>
                    )}
                    {/* 🧑 human row: mean dimension score + verdict (E-05) */}
                    {cell?.human_rated ? (
                      <div className="text-[10px] mt-0.5 text-gray-600 tabular-nums" title="human annotation: mean dimension score + verdict">
                        <span>🧑</span>
                        {cell.human_mean != null && <span className="ml-0.5">{cell.human_mean}{cell.human_std != null && <span className="text-gray-400">±{cell.human_std}</span>}</span>}
                        <span className={`ml-0.5 ${cell.human_approve === cell.human_rated ? 'text-green-600' : cell.human_approve ? 'text-amber-600' : 'text-red-600'}`}
                          title={`${cell.human_approve}/${cell.human_rated} approved`}>
                          {cell.human_approve === cell.human_rated ? '✓' : cell.human_approve ? '~' : '✗'}
                        </span>
                      </div>
                    ) : null}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-xs text-gray-400 mt-2">🔩 run outcome + ✔pass/total executable checker (✓ success · ✗ failed · ⚙ preprocessing · … running · ⏳ evaluating · · pending · s skipped · ⊘ excluded: infrastructure decided the outcome, so the run is counted but never averaged) · ⚖️ LLM judge (q = outcome quality · t = process trajectory) · 🧑 human (mean score + ✓/✗ verdict) · ±σ = spread across runs · hover q/t for the per-dimension/axis breakdown — click a cell for run details</div>
      {retiredConfigs.length > 0 && (
        <div className="text-xs text-gray-500 mt-2 border-t pt-2">
          <span className="font-medium text-gray-600">Retired configurations:</span>{' '}
          {retiredConfigs.map((c, i) => (
            <span key={c.config_key} className="line-through text-gray-400" title={`${c.config_key} — retired ${c.retired_at}`}>
              {c.label}
              {i < retiredConfigs.length - 1 ? ', ' : ''}
            </span>
          ))}
          <span className="text-gray-400">
            {' '}— out of the matrix and out of the report. Their runs are kept; see them in Runs → include retired.
          </span>
        </div>
      )}
    </div>
  )
}

// E-17 judge-trust badge: connects the calibration pillar (judge↔human agreement)
// to the experiment's A/B conclusions. Workspace-global, surfaced here on the report.
function JudgeTrustBadge() {
  const { data: badge } = useQuery({
    queryKey: ['judge-calibration-badge'],
    queryFn: () => qualityApi.getJudgeCalibrationBadge(),
  })
  if (!badge) return null
  if (!badge.calibrated) {
    return (
      <Link to="/calibration" title="Judge not yet calibrated against human annotation"
        className="text-xs px-2 py-1 rounded border border-dashed border-gray-300 text-gray-400 hover:text-gray-600">
        judge: not calibrated
      </Link>
    )
  }
  const k = badge.overall_kappa
  const tone = badge.passed ? 'border-green-300 bg-green-50 text-green-700' : 'border-amber-300 bg-amber-50 text-amber-700'
  return (
    <Link to="/calibration" className={`text-xs px-2 py-1 rounded border ${tone}`}
      title={`Judge↔human agreement: Cohen's κ over ${badge.sample_size ?? '—'} ratings from ${badge.n_humans ?? '—'} annotator(s)`}>
      judge κ {k == null ? '—' : k.toFixed(2)}{badge.passed ? ' ✓' : ' ⚠'}
    </Link>
  )
}

// Per-experiment judge↔human calibration (E-17), scoped to THIS experiment's
// annotated runs — distinct from the workspace-global JudgeTrustBadge. Empty state
// guides the user to annotate runs (Annotate tab in a run drill-down) so the κ
// becomes about this experiment instead of prior ones.
function JudgeHumanCalibration({ cal, checkerHuman }: {
  cal?: ExperimentReport['judge_calibration']
  checkerHuman?: ExperimentReport['checker_human']
}) {
  const k = cal?.overall?.cohen_kappa
  const agree = cal?.overall?.agreement_pct
  const hasData = !!cal?.available && (cal?.sample_size ?? 0) > 0
  const ch = checkerHuman
  return (
    <section>
      <h3 className="font-semibold text-gray-900 mb-2">
        Agreement with the human gold <span className="text-xs text-gray-400 font-normal">how each oracle compares to manual ratings on this experiment's annotated runs</span>
      </h3>
      {ch?.available && (
        <div className="bg-white border rounded-lg p-3 text-sm mb-2 flex flex-wrap items-center gap-x-5 gap-y-1">
          <span className="font-medium text-gray-700">Checker ↔ human</span>
          <span>κ <span className="font-semibold text-gray-800">{ch.kappa == null ? '—' : ch.kappa.toFixed(2)}</span></span>
          <span className="text-gray-500">verdict agreement {ch.agreement == null ? '—' : `${(ch.agreement * 100).toFixed(0)}%`}</span>
          <span className="text-gray-400">n={ch.n}</span>
          {ch.cells.pass_reject > 0 && <span className="text-amber-700" title="checker passed but the human rejected — checker over-credits vs the human gold">over-credit {ch.cells.pass_reject}</span>}
          {ch.cells.fail_approve > 0 && <span className="text-blue-700" title="checker failed but the human approved — checker false-negative vs the human gold">false-negative {ch.cells.fail_approve}</span>}
          <span className="text-gray-400 italic">the checker is the outcome ground truth here, yet still disagrees with the human gold</span>
        </div>
      )}
      {!hasData ? (
        <div className="bg-white border rounded-lg p-4 text-sm text-gray-500">
          No human ratings on this experiment yet. Open a run (click a matrix cell) → <span className="font-medium">Annotate</span> tab,
          score the same dimensions the judge did, and this section will show how well the LLM judge agrees with you (Cohen's κ,
          per-dimension correlation). The workspace badge above mixes all experiments; this one is scoped to these runs only.
        </div>
      ) : (
        <div className="bg-white border rounded-lg p-4 space-y-3">
          <div className="font-medium text-gray-700">Judge ↔ human</div>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
            <span>Overall <span className="font-semibold text-gray-800">κ {k == null ? '—' : k.toFixed(2)}</span>
              {cal?.overall?.reliable ? <span className="text-green-700"> ✓ reliable</span> : <span className="text-amber-600"> ⚠ below {cal?.threshold_kappa}</span>}</span>
            <span className="text-gray-500">verdict agreement {agree == null ? '—' : `${(agree * 100).toFixed(0)}%`}</span>
            <span className="text-gray-400">{cal?.n_records} annotated run(s) · {cal?.sample_size} dim-ratings · {cal?.n_humans} annotator(s)</span>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-3 py-2">Dimension</th>
                <th className="px-3 py-2">n</th>
                <th className="px-3 py-2" title="Cohen's kappa — chance-corrected agreement on the verdict (0–1; ≥ threshold = reliable)">κ</th>
                <th className="px-3 py-2" title="Pearson correlation, judge vs human scores (−1…1; 1 = perfect linear agreement)">Pearson</th>
                <th className="px-3 py-2" title="Spearman rank correlation, judge vs human scores (−1…1)">Spearman</th>
                <th className="px-3 py-2" title="mean score the LLM judge gave on this dimension (annotated runs)">Judge avg</th>
                <th className="px-3 py-2" title="mean score the human gave on this dimension (annotated runs)">Human avg</th>
                <th className="px-3 py-2" title="Judge avg − Human avg; 0 = unbiased, + = judge over-credits, − = under-credits (±0.5 signals bias)">Bias</th>
                <th className="px-3 py-2">Reliable</th>
              </tr>
            </thead>
            <tbody>
              {cal!.dimensions.map((d) => (
                <tr key={d.key} className="border-t">
                  <td className="px-3 py-2 text-gray-700">{d.name}</td>
                  <td className="px-3 py-2 text-gray-500">{d.n}</td>
                  <td className="px-3 py-2">
                    {d.cohen_kappa == null ? '—' : d.cohen_kappa.toFixed(2)}
                    {/* SPA-62: the gate acts on the point estimate; at these n the
                        interval routinely reaches across the 0.4 and 0.6 lines, and
                        a reader cannot judge the gate without seeing that. */}
                    {d.cohen_kappa_ci && (
                      <span className="ml-1 text-[10px] text-gray-400"
                        title={`95% bootstrap interval over ${d.cohen_kappa_ci.n} ratings, resampling the judge–human PAIRS.`}>
                        [{d.cohen_kappa_ci.lo.toFixed(2)}, {d.cohen_kappa_ci.hi.toFixed(2)}]
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">{d.pearson == null ? '—' : d.pearson.toFixed(2)}</td>
                  <td className="px-3 py-2">{d.spearman == null ? '—' : d.spearman.toFixed(2)}</td>
                  <td className="px-3 py-2 text-gray-600 font-medium">{d.judge_mean == null ? '—' : d.judge_mean.toFixed(1)}</td>
                  <td className="px-3 py-2 text-gray-600 font-medium">{d.human_mean == null ? '—' : d.human_mean.toFixed(1)}</td>
                  <td className={`px-3 py-2 ${(d.mean_bias ?? 0) > 0.5 ? 'text-amber-600' : (d.mean_bias ?? 0) < -0.5 ? 'text-blue-600' : 'text-gray-500'}`}>
                    {d.mean_bias == null ? '—' : (d.mean_bias > 0 ? '+' : '') + d.mean_bias.toFixed(1)}
                  </td>
                  <td className="px-3 py-2">
                    {d.status === 'insufficient_data'
                      ? <span className="text-gray-400" title="need ≥3 ratings">n/a</span>
                      : d.reliable ? <span className="text-green-700">✓</span> : <span className="text-amber-600">⚠</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// Judge↔Human per-cell triangulation scatter (moved here from the Progress tab so
// it sits under "Agreement with the human gold"): every cell with both a judge (E-02
// quality) and a human (E-05) score, colored by the executable checker verdict so
// checker false-negatives (✗ checker, high judge+human) jump off the y=x line.
function JudgeHumanScatter({ matrix, configurations, verifiable }: {
  matrix: ExperimentDetailType['matrix']
  configurations: ExperimentDetailType['configurations']
  verifiable: boolean
}) {
  const anyHuman = matrix.some((c) => (c.human_rated ?? 0) > 0)
  const labelOf = new Map(configurations.map((c) => [c.config_key, c.label || c.config_key]))
  const triPoints = matrix
    .filter((c) => c.quality_mean != null && c.human_mean != null)
    .map((c) => {
      const total = c.external_total ?? 0
      return {
        judge: c.quality_mean as number,
        human: c.human_mean as number,
        label: `${labelOf.get(c.config_key) ?? c.config_key} · ${c.case_key}`,
        checker: total === 0 ? 'none' : (c.external_pass ?? 0) >= total ? 'pass' : 'fail',
      }
    })
  if (!(anyHuman && triPoints.length >= 2)) return null
  return (
    <section>
      <h3 className="font-semibold text-gray-900 mb-2">
        ⚖️ Judge ↔ 🧑 Human <span className="text-xs text-gray-400 font-normal">per cell · quality vs human · points on the dashed diagonal = agreement</span>
      </h3>
      <div className="bg-white border rounded-lg p-3">
        <ResponsiveContainer width="100%" height={300}>
          <ScatterChart margin={{ top: 10, right: 20, bottom: 24, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis type="number" dataKey="judge" name="judge" domain={[0, 10]} tick={{ fontSize: 11 }}
              label={{ value: '⚖️ judge quality', position: 'insideBottom', offset: -12, fontSize: 11 }} />
            <YAxis type="number" dataKey="human" name="human" domain={[0, 10]} tick={{ fontSize: 11 }}
              label={{ value: '🧑 human', angle: -90, position: 'insideLeft', fontSize: 11 }} />
            <ReferenceLine segment={[{ x: 0, y: 0 }, { x: 10, y: 10 }]} stroke="#9ca3af" strokeDasharray="4 4" />
            <Tooltip cursor={{ strokeDasharray: '3 3' }}
              content={({ payload }) => (payload && payload.length ? (
                <div className="bg-white border rounded px-2 py-1 text-xs shadow">
                  <div className="font-medium">{payload[0].payload.label}</div>
                  <div>⚖️ {payload[0].payload.judge} · 🧑 {payload[0].payload.human} · checker {payload[0].payload.checker}</div>
                </div>
              ) : null)} />
            <Scatter data={triPoints}>
              {triPoints.map((p, i) => (
                <Cell key={i} fill={p.checker === 'fail' ? '#dc2626' : p.checker === 'pass' ? '#16a34a' : '#3b82f6'} />
              ))}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
        <div className="text-xs text-gray-400 mt-1">
          {verifiable ? '🟢 checker ✓ · 🔴 checker ✗ · ' : ''}🔵 no checker · dashed = perfect agreement. Points well ABOVE the diagonal where 🔴 = checker false-negatives (judge + human say good, checker failed).
        </div>
      </div>
    </section>
  )
}

// SPA-88: the reliability gate, acting. SPA-76 badged the axes and SPA-79 rescued
// the rank-consistent ones — but nothing acted on either badge, so an axis the
// calibrator had called unreliable still moved every mean, every Pareto point and
// every "significant" row. The trusted view recomputes those from the axes that
// cleared the gate, and sits NEXT TO the raw one rather than replacing it:
// quarantining an axis is a claim about the JUDGE, and the reader is owed the
// unfiltered numbers to check that claim against.
function TrustedViewToggle({ report, view, setView }: {
  report: ExperimentReport
  view: 'raw' | 'trusted'
  setView: (v: 'raw' | 'trusted') => void
}) {
  const t = report.trusted
  if (!t) return null
  const names = (rows: ExperimentTrustAxis[]) => rows.map((r) => r.name).join(', ')
  const rankOnly = [...t.outcome_axes.rank_only, ...t.trajectory_axes.rank_only]
  const dropped = [...t.outcome_axes.excluded, ...t.trajectory_axes.excluded]
  // "The judge disagrees" and "nobody ever checked" both keep an axis out of the
  // trusted view, and they are not the same claim about it — so they are not the
  // same sentence either.
  const disagrees = dropped.filter((a) => a.status === 'unreliable')
  const unknown = dropped.filter((a) => a.status !== 'unreliable')
  const nAxes = t.outcome_axes.n_axes + t.trajectory_axes.n_axes
  if (!t.available) {
    return (
      <div className="border border-gray-200 bg-gray-50 rounded-lg p-3 text-xs text-gray-500 max-w-4xl">
        <span className="font-medium text-gray-700">No trusted view.</span> Not one of the {nAxes} judged
        axes here has a calibration source that clears the gate, so there is nothing to recompute a
        trusted aggregate from. The numbers below are the judge's, unverified — which is a different
        thing from verified-bad.
      </div>
    )
  }
  return (
    <div className="border rounded-lg p-3 bg-white">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-xs font-medium text-gray-600 uppercase tracking-wide">View</span>
        <div className="flex rounded-lg border overflow-hidden text-xs">
          {([['raw', 'Raw — every axis'], ['trusted', 'Trusted — gated axes only']] as const).map(([v, label]) => (
            <button key={v} onClick={() => setView(v)}
              className={`px-2.5 py-1 ${view === v ? 'bg-blue-600 text-white' : 'bg-white hover:bg-gray-50'}`}>
              {label}
            </button>
          ))}
        </div>
        <span className="text-xs text-gray-400">
          affects Summary quality/trajectory, Pareto, leaderboard and significance — never success rate, cost, tokens or the checker
        </span>
      </div>
      <p className="text-[11px] text-gray-500 mt-2 max-w-4xl leading-relaxed">
        {dropped.length === 0 && (
          <><span className="font-medium text-gray-700">Every calibrated axis cleared the gate.</span>{' '}</>
        )}
        {disagrees.length > 0 && (
          <>
            <span className="font-medium text-red-700">{disagrees.length} of {nAxes} axes are quarantined</span>{' '}
            ({names(disagrees)}) — below κ {t.policy.moderate_kappa} and with no rank rescue either, so the
            judge agrees with the human neither on level nor on order.{' '}
          </>
        )}
        {unknown.length > 0 && (
          <>
            <span className="font-medium text-gray-700">{unknown.length} of {nAxes} axes are unverified</span>{' '}
            ({names(unknown)}) — no calibration source, or too few rated pairs to say anything either way.
            Unknown, not known-bad; the trusted view leaves them out because it can only stand behind what
            was checked.{' '}
          </>
        )}
        {dropped.length > 0 && (
          <>They drive no mean, no frontier and no verdict in the trusted view.{' '}</>
        )}
        {rankOnly.length > 0 && (
          <>
            <span className="font-medium text-amber-700">{rankOnly.length} rank-only</span> ({names(rankOnly)}):
            a judge that orders runs like the human but scores them on a shifted scale. It appears in the
            trusted view only as its own rank test (Mann-Whitney on its own scores) — not in a mean, a
            frontier or the leaderboard, because rescaling it without changing a single ordering the
            calibration validated would move all three.{' '}
          </>
        )}
        {t.dropped.significant_rows > 0 && (
          <>
            Gating removed <span className="font-medium text-gray-700">{t.dropped.significant_rows} “significant”
            row(s)</span> ({t.dropped.significant_metrics.join(', ')})
            {t.dropped.demoted_rows > 0 && <> and demoted {t.dropped.demoted_rows} more</>} — findings that
            existed only because an axis nobody can trust was allowed to vote.
          </>
        )}
      </p>
    </div>
  )
}

function ReportTab({ id, isTerminal, detail }: { id: string; isTerminal: boolean; detail: ExperimentDetailType }) {
  const queryClient = useQueryClient()
  const [method, setMethod] = useState<'bt' | 'elo'>('bt')
  const [refreshing, setRefreshing] = useState(false)
  const { data: report, isLoading } = useQuery({
    queryKey: ['experiment-report', id, method],
    queryFn: () => experimentsApi.report(id, { method }),
    refetchInterval: isTerminal ? false : 10000,
  })
  const onRefresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await experimentsApi.report(id, { method, refresh: true })
      queryClient.setQueryData(['experiment-report', id, method], fresh)
    } finally {
      setRefreshing(false)
    }
  }
  if (isLoading || !report) return <div className="text-sm text-gray-500 p-4">Assembling report…</div>
  return <ReportView report={report} method={method} setMethod={setMethod} onRefresh={onRefresh} refreshing={refreshing} detail={detail} />
}

function ReportView({ report, method, setMethod, onRefresh, refreshing, detail }: {
  report: ExperimentReport
  method: 'bt' | 'elo'
  setMethod: (m: 'bt' | 'elo') => void
  onRefresh: () => void
  refreshing: boolean
  detail: ExperimentDetailType
}) {
  const colorByConfig = new Map(
    report.summary.per_config.map((c, i) => [c.config_key, CONFIG_COLORS[i % CONFIG_COLORS.length]]),
  )
  // Mean agent steps per config (E-06 trace cleaner). Surfaced in the Summary
  // table; the rest of the cleaned-trace stats (compression / cleaned tokens)
  // are deferred until the trace-compression rework.
  const stepsByConfig = new Map(
    (report.trace_stats?.per_config ?? []).map((c) => [c.config_key, c.steps_mean]),
  )
  // Executable checker pass-rate per config (ground-truth outcome on verifiable
  // benches). Folded into the Summary next to the agent Success rate (SPA-68: the
  // two differ — Success is run-completion, Pass rate is the checker verdict).
  const passRateByConfig = new Map(
    (report.external?.per_config ?? []).map((c) => [c.config_key, c.pass_rate]),
  )
  const downloadJson = () => {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `experiment-report-${report.generated_at}.json`
    a.click()
    URL.revokeObjectURL(url)
  }
  // Verifiable bench (executable checker = outcome ground truth): the outcome
  // judge (E-02) is not the eval — it's the audited subject. Hide its scores
  // (Summary "Quality" column + the weighted_score/dim:* significance rows) and
  // keep only the trajectory (E-07) signal. (SPA-68)
  const verifiable = !!report.external?.available
  const isOutcomeMetric = (m: string) => m === 'weighted_score' || m.startsWith('dim:')
  // SPA-88: raw keeps every axis; trusted is recomputed from the ones the
  // calibrator cleared. The toggle swaps the numbers that a quarantined axis
  // could have moved — Summary quality/trajectory, Pareto, leaderboard,
  // significance — and nothing else: success rate, cost, tokens and the
  // checker's pass rate are not the judge's opinion and are never gated.
  const [view, setView] = useState<'raw' | 'trusted'>('raw')
  const trusted = report.trusted
  const trustedOn = view === 'trusted' && !!trusted?.available
  const trustedByConfig = new Map(
    (trusted?.summary.per_config ?? []).map((c) => [c.config_key, c]),
  )
  const pareto = trustedOn ? trusted!.pareto : report.pareto
  const leaderboard = trustedOn ? trusted!.leaderboard : report.leaderboard
  const significanceRows = trustedOn ? trusted!.significance : report.significance
  const significanceCorrection = trustedOn
    ? trusted!.significance_correction
    : report.significance_correction
  const visibleSignificance = verifiable
    ? significanceRows.filter((s) => !isOutcomeMetric(s.metric))
    : significanceRows
  return (
    <div className="space-y-6">
      {/* SPA-84: pinning what a config resolved to is only worth doing if the
          reader is told when it stops being true. */}
      {!!report.config_drift?.length && (
        <div className="border border-amber-300 bg-amber-50 rounded-lg p-3 text-sm">
          <p className="font-medium text-amber-800">
            Configuration drift — what these conditions mean has changed since the experiment started
          </p>
          <p className="text-xs text-amber-700 mt-1">
            The matrix fingerprint covers ids, not the contents behind them. A template edited mid-flight
            or a model row repointed at another vendor changes the condition without changing the id, so
            these results may not be comparing what the labels say.
          </p>
          <ul className="mt-2 space-y-1">
            {report.config_drift.map((d) => (
              <li key={d.config_key} className="text-xs text-amber-900">
                <span className="font-medium">{d.label || d.config_key}</span>
                {/* The stronger signal: runs of the SAME cell did not all execute
                    under the same thing, whatever the pin says now. Reported per
                    case — the resolved tool set legitimately differs between cases. */}
                {d.core_conditions && d.core_conditions.length > 1 && (
                  <span className="ml-1 font-medium text-red-700">
                    — its runs used {d.core_conditions.length} different model/prompt/image
                    combinations
                  </span>
                )}
                {d.split_cases && Object.keys(d.split_cases).length > 0 && (
                  <span className="ml-1 font-medium text-red-700">
                    — {Object.keys(d.split_cases).length} case(s) ran under more than one
                    condition ({Object.keys(d.split_cases).slice(0, 3).join(', ')}
                    {Object.keys(d.split_cases).length > 3 ? ', …' : ''})
                  </span>
                )}
                {Object.keys(d.changed).length > 0 && (
                  <>
                    {' — '}
                    {Object.entries(d.changed).map(([field, v], i, arr) => (
                      <span key={field}>
                        {field}:{' '}
                        <code className="text-amber-700 break-all">{String(v.pinned ?? '—').slice(0, 20)}</code> →{' '}
                        <code className="text-amber-700 break-all">{String(v.current ?? '—').slice(0, 20)}</code>
                        {i < arr.length - 1 ? '; ' : ''}
                      </span>
                    ))}
                  </>
                )}
                {d.resolved_at && (
                  <span className="text-amber-600"> (pinned {new Date(d.resolved_at).toLocaleString()})</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex items-center justify-end gap-2">
        <span className="text-xs text-gray-400 mr-auto">
          assembled {new Date(report.generated_at).toLocaleString()}
          {report.input_revision != null && ` · from revision ${report.input_revision}`}
          {report.selection && report.selection !== 'latest_valid' && ` · ${report.selection}`}
        </span>
        <JudgeTrustBadge />
        <button onClick={onRefresh} disabled={refreshing} title="Re-assemble report (bypass cache)"
          className="flex items-center gap-1.5 px-2.5 py-1.5 border rounded-lg hover:bg-gray-50 text-xs disabled:opacity-50">
          <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} /> {refreshing ? 'Re-assembling…' : 'Re-assemble'}
        </button>
        <button onClick={downloadJson} title="Download assembled report as JSON"
          className="flex items-center gap-1.5 px-2.5 py-1.5 border rounded-lg hover:bg-gray-50 text-xs">
          <Download className="h-3.5 w-3.5" /> JSON
        </button>
      </div>
      {report.partial && (
        <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
          Partial report — the experiment is still running ({report.n_terminal_runs} runs settled).
        </div>
      )}
      {!!report.exclusions?.contaminated && (
        <div className="text-xs text-gray-600 bg-gray-50 border rounded-lg px-3 py-2">
          <span className="font-medium">
            {report.exclusions.contaminated} run{report.exclusions.contaminated === 1 ? '' : 's'} excluded
          </span>{' '}
          — infrastructure, not the model, decided their outcome:{' '}
          {Object.entries(report.exclusions.by_type)
            .map(([type, n]) => `${n} × ${EXCLUSION_LABELS[type] ?? type}`)
            .join(', ')}
          .{' '}
          {!!report.exclusions.by_config.length && (
            <span className="text-gray-500">
              Hit{' '}
              {report.exclusions.by_config.map((c) => `${c.label} (${c.contaminated})`).join(', ')}
              .{' '}
            </span>
          )}
          <span className="text-gray-500">
            Every number below is computed without them, so <span className="font-medium">Success</span> reads
            «of the runs that measured the model», not «of the runs attempted».
          </span>
        </div>
      )}

      <TrustedViewToggle report={report} view={view} setView={setView} />

      <section>
        <h3 className="font-semibold text-gray-900 mb-2">
          Summary
          {trustedOn && <span className="ml-2 text-xs font-normal text-green-700">trusted axes only</span>}
        </h3>
        <div className="bg-white border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-3 py-2">Configuration</th>
                <th className="px-3 py-2">Runs</th>
                <th className="px-3 py-2" title="Agent run-completion rate (settled SUCCESS ÷ settled): the run finished without the harness marking it failed. NOT correctness — on verifiable benches see Pass rate (the executable checker).">Success</th>
                {verifiable && <th className="px-3 py-2" title="Executable checker pass-rate (gold.external_eval) — the GROUND-TRUTH outcome. Differs from Success: a run can finish cleanly (Success) yet fail the checker.">Pass rate</th>}
                {!verifiable && <th className="px-3 py-2">Quality</th>}
                <th className="px-3 py-2">Trajectory</th>
                <th className="px-3 py-2" title="Mean number of agent steps in the trace (trace cleaner; lower = more direct)">Steps avg</th>
                <th className="px-3 py-2" title="Effort = total LLM tokens (input+output) per run — the confound-free effort signal. See the Effort section for difficulty-normalized ×median.">Effort (tok)</th>
                <th className="px-3 py-2">Cost avg</th>
                <th className="px-3 py-2" title="Wall-clock seconds — POLLUTED by provider throttling + sleep/waits; not a clean effort signal. Use Effort (tokens) instead.">Wall-clock ⚠</th>
              </tr>
            </thead>
            <tbody>
              {report.summary.per_config.map((c) => (
                <tr key={c.config_key} className="border-t">
                  <td className="px-3 py-2 font-medium" style={{ color: colorByConfig.get(c.config_key) }} title={c.config_key}>
                    {c.label}
                  </td>
                  <td className="px-3 py-2">{c.n_runs}</td>
                  <td className="px-3 py-2">{c.success_rate != null ? `${(c.success_rate * 100).toFixed(0)}%` : '—'}</td>
                  {verifiable && (
                    <td className="px-3 py-2 font-semibold">
                      {passRateByConfig.get(c.config_key) != null ? `${(passRateByConfig.get(c.config_key)! * 100).toFixed(0)}%` : '—'}
                    </td>
                  )}
                  {!verifiable && (
                    <td className="px-3 py-2">
                      {fmt(trustedOn ? trustedByConfig.get(c.config_key)?.quality_mean ?? null : c.quality_mean)}
                    </td>
                  )}
                  <td className="px-3 py-2">
                    {fmt(trustedOn ? trustedByConfig.get(c.config_key)?.trajectory_mean ?? null : c.trajectory_mean)}
                  </td>
                  <td className="px-3 py-2">{stepsByConfig.get(c.config_key) != null ? stepsByConfig.get(c.config_key)!.toFixed(1) : '—'}</td>
                  <td className="px-3 py-2 font-medium">{fmtTokens(c.tokens_mean)}
                    {c.rel_effort != null && <span className={`ml-1 text-xs ${relEffortStyle(c.rel_effort)}`}>×{c.rel_effort.toFixed(2)}</span>}
                  </td>
                  <td className="px-3 py-2">${fmt(c.cost_mean, 3)}</td>
                  <td className="px-3 py-2 text-gray-400">{c.duration_mean != null ? `${Math.round(c.duration_mean)}s` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {report.effort?.available && (
          <p className="text-[11px] text-gray-400 mt-2 max-w-4xl">
            <span className="font-medium">Effort (tok)</span> = total LLM tokens per run — the deterministic effort signal;
            <span className="font-medium"> ×median</span> normalizes it by the per-case median across configs
            (<span className="text-amber-700">{'>'}1</span> heavier than typical, <span className="text-green-700">{'<'}1</span> lighter),
            so a config that only ran hard cases isn't penalised. <span className="font-medium">Wall-clock ⚠</span> is polluted by provider
            throttling and sleep/wait — reference only, not a skill signal. <span className="font-medium">Steps avg</span> = full agent steps
            (reasoning + tool calls), which is why it exceeds the raw tool-call count.
            {report.effort.cost_available
              ? ' Cost is shown where the provider prices per token.'
              : ' No per-token pricing here ($0) — tokens are the only honest effort signal.'}
          </p>
        )}
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
      <section>
        <h3 className="font-semibold text-gray-900 mb-2">
          Quality profile heatmap <span className="text-xs text-gray-400 font-normal">per-dimension outcome judge, success-only{verifiable ? ' · ⚠ audited subject, not the verdict' : ''}</span>
        </h3>
        {verifiable && (
          <p className="text-[11px] text-amber-700/90 mb-2 max-w-3xl">
            ⚠ On this verifiable bench the outcome judge is the <span className="font-medium">audited subject</span>, not the source of truth — the executable checker is (see <span className="font-medium">Pass rate</span> in Summary). Shown for inspection; not weighed in conclusions.
          </p>
        )}
        {report.heatmap.dimensions.length === 0 ? (
          <p className="text-sm text-gray-500">No rubric dimension scores yet (configure a judge model to score runs).</p>
        ) : (
          <>
          <div className="bg-white border rounded-lg overflow-x-auto p-3">
            <table className="text-sm border-separate" style={{ borderSpacing: 3 }}>
              <thead>
                <tr>
                  <th className="text-left text-xs text-gray-500 px-2">config</th>
                  {report.heatmap.dimensions.map((d) => {
                    const rel = report.outcome_axis_reliability?.axes?.[d]
                    const q = rel?.status === 'unreliable'
                    const dim = q || rel?.status === 'not_calibrated'
                    return (
                      <th key={d} className={`text-xs font-normal px-2 ${dim ? 'text-gray-400' : 'text-gray-500'}`}
                        title={report.heatmap.dimension_labels?.[d]}>
                        <span className={q ? 'line-through' : ''}>
                          {(report.heatmap.dimension_labels?.[d] || d).replace(/_/g, ' ')}
                        </span>
                        <ReliabilityBadge a={rel} />
                      </th>
                    )
                  })}
                  <th className="text-xs text-gray-700 font-medium px-2">weighted</th>
                </tr>
              </thead>
              <tbody>
                {report.heatmap.rows.map((row) => (
                  <tr key={row.config_key}>
                    <td className="text-xs font-medium px-2 whitespace-nowrap" title={row.config_key}>{row.label}</td>
                    {report.heatmap.dimensions.map((d) => {
                      const cell = row.cells[d]
                      const q = report.outcome_axis_reliability?.axes?.[d]?.status === 'unreliable'
                      return (
                        <td key={d} className="rounded px-3 py-2 text-center text-sm font-medium"
                          style={q ? { backgroundColor: '#f3f4f6', color: '#9ca3af' } : heatStyle(cell?.mean)}
                          title={cell ? `n=${cell.n}${cell.std != null ? ` · std=${cell.std}` : ''}${q ? ' · axis quarantined: outcome judge unreliable here' : ''}` : ''}>
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
          {report.outcome_axis_reliability?.available && (
            <p className="text-[11px] text-gray-400 mt-1 max-w-3xl">
              <span className="font-medium">Reliability gate (outcome):</span> each rubric axis is badged by judge↔human agreement —{' '}
              <span className="text-green-700 font-semibold">✓</span> reliable (κ≥{report.outcome_axis_reliability.reliable_kappa}),{' '}
              <span className="text-amber-600 font-semibold">~</span> moderate ({report.outcome_axis_reliability.directional_kappa}–{report.outcome_axis_reliability.reliable_kappa})
              or rank-only (κ below it but rank ρ≥{report.outcome_axis_reliability.rank_rho ?? 0.5} — a scale-shifted judge: usable for a rank test, never for a mean),{' '}
              <span className="text-red-600 font-semibold">⚠</span> unreliable,{' '}
              <span className="text-gray-400 font-semibold">n/a</span> not calibrated or too few rated pairs.{' '}
              <span className="font-medium">Greyed/struck (⚠) axes are below the bar — shown for completeness; switch the view to Trusted to see the report without them.</span>
            </p>
          )}
          </>
        )}
      </section>

      <section>
        <h3 className="font-semibold text-gray-900 mb-2">
          Trajectory profile heatmap <span className="text-xs text-gray-400 font-normal">6-axis process judge per config</span>
        </h3>
        {report.trajectory_heatmap.axes.length === 0 ? (
          <p className="text-sm text-gray-500">No trajectory scores yet (the 6-axis process judge runs on settled runs with a trace).</p>
        ) : (
          <>
          <div className="bg-white border rounded-lg overflow-x-auto p-3">
            <table className="text-sm border-separate" style={{ borderSpacing: 3 }}>
              <thead>
                <tr>
                  <th className="text-left text-xs text-gray-500 px-2">config</th>
                  {report.trajectory_heatmap.axes.map((a) => {
                    const rel = report.axis_reliability?.axes?.[a]
                    const q = rel?.status === 'unreliable'
                    const dim = q || rel?.status === 'not_calibrated'
                    return (
                      <th key={a} className={`text-xs font-normal px-2 ${dim ? 'text-gray-400' : 'text-gray-500'}`}
                        title={report.trajectory_heatmap.axis_labels[a]}>
                        <span className={q ? 'line-through' : ''}>
                          {(report.trajectory_heatmap.axis_labels[a] || a).replace(/_/g, ' ')}
                        </span>
                        <ReliabilityBadge a={rel} />
                      </th>
                    )
                  })}
                  {(() => {
                    const agg = trajectoryAggregateStatus(report)
                    const q = agg === 'unreliable'
                    return (
                      <th className={`text-xs font-medium px-2 ${q ? 'text-gray-400' : 'text-gray-700'}`}
                        title={agg ? `Aggregate of the 6 axes — only as reliable as its weakest calibrated axis (${RELIABILITY_META[agg].word}).` : undefined}>
                        <span className={q ? 'line-through' : ''}>overall</span>
                        {agg && (
                          <span className={`ml-1 text-[10px] font-semibold ${RELIABILITY_META[agg].cls}`}>{RELIABILITY_META[agg].glyph}</span>
                        )}
                      </th>
                    )
                  })()}
                </tr>
              </thead>
              <tbody>
                {report.trajectory_heatmap.rows.map((row) => (
                  <tr key={row.config_key}>
                    <td className="text-xs font-medium px-2 whitespace-nowrap" title={row.config_key}>{row.label}</td>
                    {report.trajectory_heatmap.axes.map((a) => {
                      const cell = row.cells[a]
                      const q = report.axis_reliability?.axes?.[a]?.status === 'unreliable'
                      return (
                        <td key={a} className="rounded px-3 py-2 text-center text-sm font-medium"
                          style={q ? { backgroundColor: '#f3f4f6', color: '#9ca3af' } : heatStyle(cell?.mean)}
                          title={cell ? `n=${cell.n}${cell.std != null ? ` · std=${cell.std}` : ''}${q ? ' · axis quarantined: process judge unreliable here' : ''}` : ''}>
                          {fmt(cell?.mean, 1)}
                        </td>
                      )
                    })}
                    <td className="rounded px-3 py-2 text-center text-sm font-bold"
                      style={trajectoryAggregateStatus(report) === 'unreliable'
                        ? { backgroundColor: '#f3f4f6', color: '#9ca3af' }
                        : heatStyle(row.overall_score.mean)}>
                      {fmt(row.overall_score.mean, 1)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {report.axis_reliability?.available ? (
            <p className="text-[11px] text-gray-400 mt-1 max-w-3xl">
              <span className="font-medium">Reliability gate:</span> each axis is badged by how far the process judge can be
              trusted — <span className="text-green-700 font-semibold">✓</span> reliable (κ≥{report.axis_reliability.reliable_kappa}),{' '}
              <span className="text-amber-600 font-semibold">~</span> moderate ({report.axis_reliability.directional_kappa}–{report.axis_reliability.reliable_kappa})
              or rank-only (κ below it but rank ρ≥{report.axis_reliability.rank_rho ?? 0.5} — a scale-shifted judge: usable for a rank test, never for a mean),{' '}
              <span className="text-red-600 font-semibold">⚠</span> unreliable,{' '}
              <span className="text-gray-400 font-semibold">n/a</span> not calibrated or too few rated pairs. κ here is chance-corrected agreement with a human
              (the loop axis instead anchors to the deterministic counter — see Loop detection). <span className="font-medium">Greyed/struck (⚠) axes are below the bar — shown for
              completeness; switch the view to Trusted to see the report without them.</span>
            </p>
          ) : report.axis_reliability ? (
            <p className="text-[11px] text-gray-400 mt-1 max-w-3xl">
              <span className="font-medium">Reliability gate:</span> no calibration source for these axes yet (no human axis ratings;
              the structural loop anchor needs trajectory-scored runs) — process scores are shown but <span className="font-medium">unverified</span> (n/a).
            </p>
          ) : null}
          </>
        )}
      </section>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
      {!verifiable && report.quality_gate?.available && (
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">
            Quality gate <span className="text-xs text-gray-400 font-normal">share of outcome-scored runs that cleared the critical rubric thresholds · success or failed</span>
          </h3>
          <div className="bg-white border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2">Configuration</th>
                  <th className="px-3 py-2" title="share of scored runs whose result passed every CRITICAL rubric dimension (higher is better)">Gate pass</th>
                  <th className="px-3 py-2">Passed</th>
                  <th className="px-3 py-2">Scored</th>
                  <th className="px-3 py-2" title="rubric dimensions that most often fail the gate (count of runs)">Top failing dimensions</th>
                </tr>
              </thead>
              <tbody>
                {report.quality_gate.per_config.map((c) => {
                  const failed = Object.entries(c.failed_dimensions).sort((a, b) => b[1] - a[1])
                  return (
                    <tr key={c.config_key} className="border-t align-top">
                      <td className="px-3 py-2 font-medium" title={c.config_key}>{c.label}</td>
                      <td className="px-3 py-2 font-semibold">{c.pass_rate != null ? `${(c.pass_rate * 100).toFixed(0)}%` : '—'}</td>
                      <td className="px-3 py-2 text-green-700">{c.n_pass}</td>
                      <td className="px-3 py-2 text-gray-500">{c.n}</td>
                      <td className="px-3 py-2 text-gray-600">
                        {failed.length
                          ? failed.map(([d, n]) => `${(report.heatmap.dimension_labels?.[d] || d).replace(/_/g, ' ')}: ${n}`).join(' · ')
                          : <span className="text-gray-300">—</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {report.loop_detection?.structural_available && (() => {
        const ld = report.loop_detection!
        return (
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">
            Loop detection <span className="text-xs text-gray-400 font-normal">deterministic loop counter · repeated tool-calls over the FULL trace · success or failed · lower is better</span>
          </h3>
          {ld.kappa != null && (
            <p className="text-xs text-gray-500 mb-2 max-w-3xl">
              <span className="font-medium">Judge↔counter agreement:</span> Cohen's κ {ld.kappa.toFixed(2)}
              {ld.agreement != null && <> · {(ld.agreement * 100).toFixed(0)}% raw</>} · split {ld.n_judge_only ?? 0} judge-only / {ld.n_counter_only ?? 0} counter-only.
              Framed as <span className="font-medium">different inputs</span> (trimmed + holistic judge vs full + tool-only counter), not pure miscalibration.
            </p>
          )}
          <div className="bg-white border rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2">Configuration</th>
                  <th className="px-3 py-2" title="deterministic counter: repeated tool-calls counted over the FULL untrimmed trace — LLM-free; a precision-oriented structural lower bound (may miss semantic loops)">Loop rate (counted)</th>
                  <th className="px-3 py-2" title="the LLM judge's loop_detection rate on the same runs — retired from conclusions, shown only for the judge↔counter comparison (κ above)">Loop rate (judge)</th>
                  <th className="px-3 py-2">Counted</th>
                </tr>
              </thead>
              <tbody>
                {ld.per_config.map((c) => (
                  <tr key={c.config_key} className="border-t">
                    <td className="px-3 py-2 font-medium" title={c.config_key}>{c.label}</td>
                    <td className={`px-3 py-2 font-semibold ${(c.structural_loop_rate ?? 0) > 0 ? 'text-amber-700' : 'text-gray-700'}`}
                      title={c.n_structural ? `${c.n_structural_loop} of ${c.n_structural} runs (counted)` : 'no deterministic data'}>
                      {c.structural_loop_rate != null ? `${(c.structural_loop_rate * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-600"
                      title={(c.n_judge_only != null || c.n_counter_only != null) ? `${c.n_judge_only ?? 0} judge-only / ${c.n_counter_only ?? 0} counter-only${c.kappa != null ? ` · κ ${c.kappa.toFixed(2)}` : ''}` : 'no judge loop signal'}>
                      {c.loop_rate != null ? `${(c.loop_rate * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-500">{c.n_structural ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-gray-400 mt-1 max-w-3xl">
            <span className="font-medium">Loop rate (counted)</span> is a deterministic, LLM-free detector: it counts repeated
            tool-calls — consecutive identical actions or repeated multi-step tool cycles — over the FULL untrimmed trace. It is a
            precision-oriented structural lower bound (tool-calls only; may miss semantic loops that vary their wording). The unreliable
            judge <code>loop_detection</code> axis (κ≈0 vs humans) is retired from conclusions in favour of this counter; the
            <span className="font-medium"> Loop rate (judge)</span> column and the judge↔counter κ above are shown only to expose that
            disagreement (different inputs), not used in conclusions.
          </p>
        </section>
        )
      })()}
      </div>

      {report.longitudinal?.available && (
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">
            Longitudinal <span className="text-xs text-gray-400 font-normal">quality / cost across the repetition index — do later repeats of a cell drift?</span>
          </h3>
          <div className="bg-white border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2" title="0-based repetition index of each cell">Run #</th>
                  <th className="px-3 py-2">Runs</th>
                  {!verifiable && <th className="px-3 py-2">Quality avg</th>}
                  <th className="px-3 py-2">Trajectory avg</th>
                  <th className="px-3 py-2" title="Token effort across repetitions">Tokens avg</th>
                  <th className="px-3 py-2">Cost avg</th>
                </tr>
              </thead>
              <tbody>
                {report.longitudinal.points.map((p) => (
                  <tr key={p.run_index} className="border-t">
                    <td className="px-3 py-2 font-medium">#{p.run_index + 1}</td>
                    <td className="px-3 py-2 text-gray-500">{p.n}</td>
                    {!verifiable && <td className="px-3 py-2">{fmt(p.quality_mean)}</td>}
                    <td className="px-3 py-2">{fmt(p.trajectory_mean)}</td>
                    <td className="px-3 py-2">{fmtTokens(p.tokens_mean)}</td>
                    <td className="px-3 py-2">${fmt(p.cost_mean, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
      {report.human_feedback?.available && (
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">
            Human feedback profile <span className="text-xs text-gray-400 font-normal">per-dimension human ratings · all rated runs · the third oracle</span>
          </h3>
          <div className="bg-white border rounded-lg overflow-x-auto p-3">
            <table className="text-sm border-separate" style={{ borderSpacing: 3 }}>
              <thead>
                <tr>
                  <th className="text-left text-xs text-gray-500 px-2">config</th>
                  {report.human_feedback.dimensions.map((d) => (
                    <th key={d} className="text-xs text-gray-500 font-normal px-2" title={report.human_feedback!.dimension_labels[d]}>
                      {(report.human_feedback!.dimension_labels[d] || d).replace(/_/g, ' ')}
                    </th>
                  ))}
                  <th className="text-xs text-gray-700 font-medium px-2">overall</th>
                  <th className="text-xs text-gray-500 font-normal px-2" title="approve / reject verdicts on the rated runs (· = rated, no verdict)">verdict</th>
                </tr>
              </thead>
              <tbody>
                {report.human_feedback.rows.map((row) => (
                  <tr key={row.config_key}>
                    <td className="text-xs font-medium px-2 whitespace-nowrap" title={row.config_key}>{row.label}</td>
                    {report.human_feedback!.dimensions.map((d) => {
                      const cell = row.cells[d]
                      return (
                        <td key={d} className="rounded px-3 py-2 text-center text-sm font-medium" style={heatStyle(cell?.mean)}
                          title={cell ? `n=${cell.n}${cell.std != null ? ` · std=${cell.std}` : ''}` : ''}>
                          {fmt(cell?.mean, 1)}
                        </td>
                      )
                    })}
                    <td className="rounded px-3 py-2 text-center text-sm font-bold" style={heatStyle(row.overall_score.mean)}
                      title={`n=${row.overall_score.n}${row.overall_score.std != null ? ` · std=${row.overall_score.std}` : ''}`}>
                      {fmt(row.overall_score.mean, 1)}
                    </td>
                    <td className="px-2 text-center text-xs whitespace-nowrap">
                      {row.n_rated === 0 ? <span className="text-gray-300">—</span> : (
                        <span title={`${row.n_rated} rated run(s)`}>
                          {row.verdicts.approve > 0 && <span className="text-green-700">{row.verdicts.approve}✓</span>}
                          {row.verdicts.reject > 0 && <span className="text-red-700 ml-1">{row.verdicts.reject}✗</span>}
                          {row.verdicts.none > 0 && <span className="text-gray-400 ml-1">{row.verdicts.none}·</span>}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-gray-400 mt-1 max-w-3xl">
            Raw human signal — independent of the judge↔human agreement below. Aggregated over every rated
            run (not success-only), so the verdict counts keep the rejects. Cells colour low→high like the judge heatmaps; hover for n / σ.
          </p>
        </section>
      )}

      {report.trajectory_match.available && (
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">
            Trajectory match <span className="text-xs text-gray-400 font-normal">vs canonical gold trajectory</span>
          </h3>
          <div className="bg-white border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2">Configuration</th>
                  <th className="px-3 py-2" title="% of scored runs whose trajectory matches the canonical gold trajectory within threshold">Match rate</th>
                  <th className="px-3 py-2" title="mean trajectory similarity to the gold trajectory (0–1; higher = closer)">Score mean</th>
                  <th className="px-3 py-2" title="runs that had a canonical gold trajectory to score against">Scored</th>
                </tr>
              </thead>
              <tbody>
                {report.trajectory_match.per_config.map((c) => (
                  <tr key={c.config_key} className="border-t">
                    <td className="px-3 py-2 font-medium" title={c.config_key}>{c.label}</td>
                    <td className="px-3 py-2">{c.match_rate != null ? `${(c.match_rate * 100).toFixed(0)}%` : '—'}</td>
                    <td className="px-3 py-2">{fmt(c.score_mean, 2)}</td>
                    <td className="px-3 py-2 text-gray-500">{c.n_scored}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}


      {report.judge_discrimination?.available && (
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">
            RQ2 · does the judge separate pass from fail?{' '}
            <span className="text-xs text-gray-400 font-normal">
              no threshold — the judge’s scores split by the executable verdict
            </span>
          </h3>
          <div className="bg-white border rounded-lg p-4 max-w-2xl space-y-3">
            <div className="grid grid-cols-3 gap-4 text-sm">
              <div>
                <div className="text-xs text-gray-500">median · checker passed</div>
                <div className="text-2xl font-semibold text-green-700">
                  {report.judge_discrimination.overall.median_on_pass?.toFixed(1) ?? '—'}
                </div>
                <div className="text-[11px] text-gray-400">n={report.judge_discrimination.overall.n_checker_pass}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">median · checker failed</div>
                <div className="text-2xl font-semibold text-amber-700">
                  {report.judge_discrimination.overall.median_on_fail?.toFixed(1) ?? '—'}
                </div>
                <div className="text-[11px] text-gray-400">n={report.judge_discrimination.overall.n_checker_fail}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500" title="Probability that a run the checker passed outranks one it failed. 0.5 = chance; below 0.5 = the judge ranks them backwards.">
                  AUC
                </div>
                <div className="text-2xl font-semibold text-gray-900">
                  {report.judge_discrimination.overall.auc?.toFixed(2) ?? '—'}
                </div>
                <div className="text-[11px] text-gray-400">
                  {/* The headline number over a few dozen runs reads far more
                      precise than it is when printed bare (SPA-62). */}
                  {report.judge_discrimination.overall.auc_ci && (
                    <span title="95% bootstrap interval, resampling the checker's passes and failures independently. An interval covering 0.50 means the separation is not established.">
                      [{report.judge_discrimination.overall.auc_ci.lo.toFixed(2)}, {report.judge_discrimination.overall.auc_ci.hi.toFixed(2)}]{' · '}
                    </span>
                  )}
                  {report.judge_discrimination.overall.mann_whitney?.p != null
                    ? `p=${report.judge_discrimination.overall.mann_whitney.p.toFixed(3)}`
                    : 'p — n/a'}
                </div>
              </div>
            </div>
            <p className="text-[11px] text-gray-400">
              The headline, and it has no cut-off to move: <span className="text-amber-700">median · checker failed</span>{' '}
              is the over-credit number — how well the judge scores work that demonstrably did not
              work — and AUC is how reliably it ranks a pass above a fail at all. Every other
              framing of this question depends on where a threshold is drawn, so it can be
              improved after the fact by drawing it elsewhere.
            </p>
          </div>
        </section>
      )}

      {report.rq2?.available && (
        <section>
          <h3 className="font-semibold text-gray-900 mb-2">
            RQ2 · verdict × judge{' '}
            <span className="text-xs text-gray-400 font-normal">
              illustration at ≥{report.rq2.judge_threshold}
              {report.rq2.threshold_source === 'pre_registered' ? (
                <span className="ml-1 text-green-700" title="Set in this experiment's eval_config before it ran, and frozen there — the cut-off could not have been chosen to suit the result.">
                  · pre-registered
                </span>
              ) : (
                <span className="ml-1 text-amber-600" title="No threshold was pre-registered, so the project default applies. Set eval_config.judge_threshold at creation to record the choice.">
                  · default, not pre-registered
                </span>
              )}
              {' '}— agreement{' '}
              {report.rq2.overall.agreement != null ? `${(report.rq2.overall.agreement * 100).toFixed(0)}%` : '—'}
              {report.rq2.overall.agreement_ci && (
                <span className="text-gray-400"
                  title="Wilson 95% score interval. On a 2×2 over a few dozen runs the textbook interval runs outside [0,1] and reports certainty on an empty cell; Wilson does neither.">
                  {' '}[{(report.rq2.overall.agreement_ci.lo * 100).toFixed(0)}–{(report.rq2.overall.agreement_ci.hi * 100).toFixed(0)}%]
                </span>
              )} (n={report.rq2.overall.n})
            </span>
          </h3>
          <div className="bg-white border rounded-lg p-4 max-w-md">
            <div className="grid grid-cols-[auto_1fr_1fr] gap-1 text-sm text-center">
              <div></div>
              <div className="text-xs text-gray-500 font-medium py-1">judge high</div>
              <div className="text-xs text-gray-500 font-medium py-1">judge low</div>
              <div className="text-xs text-gray-500 font-medium flex items-center justify-end pr-2">checker pass</div>
              <div className="bg-green-50 text-green-700 font-semibold py-3 rounded" title="checker passed & judge high — agree">{report.rq2.overall.cells.pass_high}</div>
              <div className="bg-amber-50 text-amber-700 font-semibold py-3 rounded" title="checker passed but judge scored low — judge under-credits">{report.rq2.overall.cells.pass_low}</div>
              <div className="text-xs text-gray-500 font-medium flex items-center justify-end pr-2">checker fail</div>
              <div className="bg-amber-50 text-amber-700 font-semibold py-3 rounded" title="judge scored high but checker failed — judge over-credits">
                {report.rq2.overall.cells.fail_high}
                {/* The report's central number. Printed as a bare count it reads
                    as a measurement; the interval says how well it is pinned. */}
                {report.rq2.overall.over_credit_ci && (
                  <div className="text-[10px] font-normal text-amber-600"
                    title="Wilson 95% interval for the over-credit RATE. On an empty cell the textbook interval would claim [0%, 0%] — certainty from no evidence.">
                    {(report.rq2.overall.over_credit_ci.p * 100).toFixed(0)}%
                    {' ['}{(report.rq2.overall.over_credit_ci.lo * 100).toFixed(0)}–{(report.rq2.overall.over_credit_ci.hi * 100).toFixed(0)}%]
                  </div>
                )}
              </div>
              <div className="bg-red-50 text-red-700 font-semibold py-3 rounded" title="checker failed & judge low — agree">{report.rq2.overall.cells.fail_low}</div>
            </div>
            <p className="text-[11px] text-gray-400 mt-2">
              {verifiable && <span className="text-gray-500">On a verifiable bench the executable checker is the verdict; the outcome judge is the audited subject here. </span>}
              Diagonal (green/red) = judge agrees with the executable checker; off-diagonal (amber) = disagreement.
              The <span className="text-amber-700">fail × judge-high</span> cell is the over-credit signal — the judge rewarding a
              result the checker rejected. This is the outcome-judge analogue of the human-calibrated κ in <span className="font-medium">Judge ↔ human</span> below.
            </p>
          </div>
          {!!report.rq2.sensitivity?.length && (
            <div className="bg-white border rounded-lg p-4 max-w-md mt-2">
              <div className="text-xs text-gray-500 mb-2">
                Sensitivity — exploratory. How the same corpus reads at neighbouring cut-offs.
              </div>
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500">
                    <th className="text-left font-medium py-1">threshold</th>
                    <th className="text-right font-medium">over-credit</th>
                    <th className="text-right font-medium">agreement</th>
                  </tr>
                </thead>
                <tbody>
                  {report.rq2.sensitivity.map((row) => (
                    <tr key={row.threshold} className={row.pre_registered ? 'font-semibold text-gray-900' : 'text-gray-500'}>
                      <td className="py-0.5">
                        ≥{row.threshold}
                        {row.pre_registered && <span className="ml-1 text-[10px] text-green-700">primary</span>}
                      </td>
                      <td className="text-right">{row.cells.fail_high}</td>
                      <td className="text-right">
                        {row.agreement != null ? `${(row.agreement * 100).toFixed(0)}%` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <SummaryRadarPanel
          title="Quality profile"
          subtitle={verifiable
            ? 'overlay · per-config dimensions (success-only) · ⚠ audited subject, not the verdict — checker is (Pass rate)'
            : 'overlay · per-config dimensions (success-only) — toggle configs · ⚠ greyed axis = outcome judge below the reliability bar'}
          axisStatus={(k) => report.outcome_axis_reliability?.axes?.[k]?.status}
          axes={report.heatmap.dimensions}
          axisLabel={(k) => report.heatmap.dimension_labels?.[k] ?? k.replace(/_/g, ' ')}
          rows={report.heatmap.rows}
          colorOf={(k) => colorByConfig.get(k)}
        />
        <SummaryRadarPanel
          title="Trajectory profile"
          subtitle="overlay · per-config axes (success-only) — toggle configs · ⚠ greyed axis = process judge below the reliability bar"
          axes={report.trajectory_heatmap.axes}
          axisLabel={(k) => report.trajectory_heatmap.axis_labels?.[k] ?? k.replace(/_/g, ' ')}
          axisStatus={(k) => report.axis_reliability?.axes?.[k]?.status}
          rows={report.trajectory_heatmap.rows}
          colorOf={(k) => colorByConfig.get(k)}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section>
          {(() => {
            const pts = pareto.points
            const costVaries = new Set(pts.map((p) => p.cost)).size > 1
            const effortVaries = new Set(pts.map((p) => p.effort ?? null)).size > 1
            // SPA-77: X axis = cost when priced, else fall back to TOKEN effort so the
            // frontier stays meaningful for un-metered ($0) providers; the bubble is
            // token effort (or caveated wall-clock when effort is already the axis).
            const xKey = costVaries ? 'cost' : 'effort'
            const xLabel = costVaries ? 'Cost ($)' : 'Effort (tokens)'
            const bubbleKey = costVaries ? 'effort' : 'time'
            return (
            <>
            <h3 className="font-semibold text-gray-900 mb-2">Pareto frontier <span className="text-xs text-gray-400 font-normal">quality × {costVaries ? 'cost' : 'token effort'} · bubble = {costVaries ? 'token effort' : 'wall-clock ⚠'} · <span className="text-green-700">green</span> = on the frontier (quality↑ · cost↓ · effort↓), grey = dominated{verifiable ? ' · *audited, not evaluator' : ''}</span></h3>
            <div className="bg-white border rounded-lg p-3 h-72">
            {!(costVaries || effortVaries) ? (
              <div className="h-full flex items-center justify-center text-center text-xs text-gray-400 px-6">
                Neither cost nor token effort varies across configs — a quality × effort frontier is degenerate.
                Compare quality via the leaderboard and heatmap instead.
              </div>
            ) : (
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 10, right: 20, bottom: 28, left: 12 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis type="number" dataKey={xKey} name={xKey} tick={{ fontSize: 11 }}
                  tickFormatter={(v) => (costVaries ? `$${Number(v).toFixed(2)}` : fmtTokens(Number(v)))}
                  label={{ value: xLabel, position: 'insideBottom', offset: -12, fontSize: 11, fill: '#6b7280' }} />
                <YAxis type="number" dataKey="quality" name="quality" domain={[0, 10]} tick={{ fontSize: 11 }}
                  label={{ value: verifiable ? 'Quality*' : 'Quality', angle: -90, position: 'insideLeft', fontSize: 11, fill: '#6b7280' }} />
                <ZAxis type="number" dataKey={bubbleKey} range={[60, 400]} name={bubbleKey} />
                <Tooltip cursor={{ strokeDasharray: '3 3' }}
                  content={({ payload }) => (payload && payload.length ? (
                    <div className="bg-white border rounded px-2 py-1 text-xs shadow">
                      <div className="font-medium">{payload[0].payload.label}</div>
                      <div>quality {fmt(payload[0].payload.quality, 1)} · {fmtTokens(payload[0].payload.effort)} tok · ${fmt(payload[0].payload.cost, 3)} · <span className="text-gray-400">{payload[0].payload.time != null ? `${Math.round(payload[0].payload.time)}s ⚠` : '—'}</span>{payload[0].payload.on_frontier ? ' · frontier' : ''}</div>
                    </div>
                  ) : null)} />
                <Legend />
                <Scatter name="frontier" data={pts.filter((p) => p.on_frontier)} fill="#16a34a">
                  <LabelList dataKey="label" position="top" offset={8} fontSize={11} fill="#15803d" />
                </Scatter>
                <Scatter name="dominated" data={pts.filter((p) => !p.on_frontier)} fill="#9ca3af">
                  <LabelList dataKey="label" position="top" offset={8} fontSize={11} fill="#6b7280" />
                </Scatter>
              </ScatterChart>
            </ResponsiveContainer>
            )}
            </div>
            </>
            )
          })()}
        </section>

        <section>
          <h3 className="font-semibold text-gray-900 mb-2">Outcome × Trajectory <span className="text-xs text-gray-400 font-normal">per run{verifiable ? ' · *outcome audited, not evaluator' : ''}</span></h3>
          <div className="bg-white border rounded-lg p-3 h-72">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 10, right: 20, bottom: 28, left: 12 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis type="number" dataKey="outcome" name="outcome" domain={[0, 10]} tick={{ fontSize: 11 }}
                  label={{ value: verifiable ? 'Outcome*' : 'Outcome', position: 'insideBottom', offset: -12, fontSize: 11, fill: '#6b7280' }} />
                <YAxis type="number" dataKey="trajectory" name="trajectory" domain={[0, 10]} tick={{ fontSize: 11 }}
                  label={{ value: 'Trajectory', angle: -90, position: 'insideLeft', fontSize: 11, fill: '#6b7280' }} />
                <Tooltip cursor={{ strokeDasharray: '3 3' }}
                  content={({ payload }) => (payload && payload.length ? (
                    <div className="bg-white border rounded px-2 py-1 text-xs shadow">
                      <div className="font-medium">{payload[0].payload.label} · {payload[0].payload.case_key} · #{(payload[0].payload.run_index ?? 0) + 1}</div>
                      <div>
                        {verifiable ? 'outcome*' : 'outcome'} {fmt(payload[0].payload.outcome, 1)} · trajectory {fmt(payload[0].payload.trajectory, 1)} ·{' '}
                        <span className={payload[0].payload.status === 'failed' ? 'text-red-600' : 'text-gray-500'}>{payload[0].payload.status}</span>
                      </div>
                    </div>
                  ) : null)} />
                <Legend />
                {report.summary.per_config.map((c) => (
                  <Scatter key={c.config_key} name={c.label || c.config_key}
                    data={report.scatter.filter((p) => p.config_key === c.config_key && p.status !== 'failed' && p.outcome != null && p.trajectory != null)}
                    fill={colorByConfig.get(c.config_key)} />
                ))}
                <Scatter name="failed (any model)" shape="cross"
                  data={report.scatter.filter((p) => p.status === 'failed' && p.outcome != null && p.trajectory != null)}
                  fill="#9ca3af" />
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
          <span className="text-xs text-gray-400">
            derived from pointwise scores, case-paired
            {trustedOn && ' · trusted view: numeric axes only — a weighted mean is still a mean, so rank-rescued axes stay out'}
          </span>
        </div>
        {leaderboard.status !== 'ok' ? (
          <p className="text-sm text-gray-500">
            {trustedOn
              ? `No leaderboard from the trusted axes alone (${leaderboard.status}) — nothing here can order these configurations.`
              : `Not enough scored runs for a leaderboard (${leaderboard.status}).`}
          </p>
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
                {leaderboard.players.map((p) => (
                  <tr key={p.player} className="border-t">
                    <td className="px-3 py-2 font-bold">{p.rank}</td>
                    <td className="px-3 py-2 font-medium" title={p.player}>{p.label}</td>
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

      <JudgeHumanCalibration cal={report.judge_calibration} checkerHuman={report.checker_human} />

      <JudgeHumanScatter matrix={detail.matrix} configurations={detail.configurations} verifiable={verifiable} />

      <section>
        <h3 className="font-semibold text-gray-900 mb-2">Statistical significance <span className="text-xs text-gray-400 font-normal">paired by case (Welch fallback); ★ = q &lt; 0.05 after correction</span></h3>
        {/* SPA-62: a table that runs dozens of tests and prints each as if it were
            the only one manufactures roughly one green row per twenty from nothing.
            How many were run, and in which family, is therefore part of the result. */}
        {significanceCorrection && (
          <p className="text-xs text-gray-500 mb-2 -mt-1 max-w-3xl">
            {significanceCorrection.n_tests} test{significanceCorrection.n_tests === 1 ? '' : 's'} run,
            corrected with Benjamini-Hochberg (FDR) within each family:{' '}
            {Object.entries(significanceCorrection.families).map(([name, f], i) => (
              <span key={name}>
                {i > 0 && ' · '}
                <span className="font-medium">{name}</span> {f.n_tests}
                {f.n_significant_uncorrected > f.n_significant && (
                  <span className="text-amber-700">
                    {' '}({f.n_significant_uncorrected - f.n_significant} lost the star to the correction)
                  </span>
                )}
              </span>
            ))}
            {significanceCorrection.n_omitted > 0 && (
              <>
                {' · '}
                <span className="text-gray-400">
                  {significanceCorrection.n_omitted} comparison{significanceCorrection.n_omitted === 1 ? '' : 's'} not testable
                  {significanceCorrection.omitted.too_few_cases
                    ? ` (fewer than ${significanceCorrection.min_cases} shared cases)`
                    : ''}
                </span>
              </>
            )}
          </p>
        )}
        {report.estimand && (
          <p className="text-xs text-gray-400 mb-2 max-w-3xl">
            Estimand: the mean within-case difference over runs that finished{' '}
            <span className="font-medium">successfully</span>, one value per (config, case) cell —
            repeated runs of a case are averaged, not counted separately.
            {report.estimand.survivor_conditioned && (() => {
              const lost = Object.entries(report.estimand.excluded_by_status).filter(([, n]) => n > 0)
              return (
                <span className="text-amber-700">
                  {' '}Survivor-conditioned: {lost.map(([k, n]) => `${k} lost ${n} run${n === 1 ? '' : 's'}`).join(', ')}
                  {' '}— {lost.length === 1 ? 'it is' : 'they are'} scored on the subset that finished.
                </span>
              )
            })()}
          </p>
        )}
        {verifiable && (
          <p className="text-xs text-gray-400 mb-2 -mt-1 max-w-3xl">
            On verifiable benches the outcome judge is the subject being audited (not the evaluator), so its metrics
            (Overall quality + dimensions) are hidden here — only Trajectory is shown. See <span className="font-medium">Pass rate</span> in the Summary for the ground-truth outcome.
          </p>
        )}
        {visibleSignificance.length > 0 && (
          <p className="text-xs text-gray-400 mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
            <span>Judge:</span>
            <span className="px-1.5 py-0.5 rounded font-medium text-blue-700 bg-blue-50">Quality</span>
            <span>= outcome rubric ·</span>
            <span className="px-1.5 py-0.5 rounded font-medium text-purple-700 bg-purple-50">Trajectory</span>
            <span>= process, 6-axis. Rows are grouped by evaluator.</span>
          </p>
        )}
        {visibleSignificance.length === 0 ? (
          <p className="text-sm text-gray-500">
            Nothing testable yet — a comparison needs at least 3 CASES scored on both sides.
            Repeated runs of the same case are averaged into it, so running one case ten times
            is still one observation.
          </p>
        ) : (
          <div className="bg-white border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
                <tr>
                  <th className="px-3 py-2">Pair</th>
                  <th className="px-3 py-2">Metric</th>
                  <th className="px-3 py-2" title="which evaluator produced this metric — outcome judge or process judge">Judge</th>
                  <th className="px-3 py-2" title="Paired = the same cases compared within themselves, which is how the matrix was built. Unpaired = too few shared cases, so Welch over the cell values.">Design</th>
                  <th className="px-3 py-2" title="Mean difference with its 95% bootstrap interval, plus the standardised effect. An interval spanning zero says the direction itself is not settled.">Effect</th>
                  <th className="px-3 py-2" title="Uncorrected p from this row's primary test">p</th>
                  <th className="px-3 py-2" title="Benjamini-Hochberg q within this row's family — p adjusted for how many tests were run. The verdict follows this.">q</th>
                  <th className="px-3 py-2">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {[...visibleSignificance]
                  .sort((x, y) =>
                    metricJudge(x.metric).label.localeCompare(metricJudge(y.metric).label) ||
                    metricLabel(x.metric).localeCompare(metricLabel(y.metric)) ||
                    `${x.a}${x.b}`.localeCompare(`${y.a}${y.b}`))
                  .map((s) => {
                    const judge = metricJudge(s.metric)
                    const agg = s.metric === 'trajectory_score' ? trajectoryAggregateStatus(report) : null
                    return (
                      <tr key={`${s.a}-${s.b}-${s.metric}`} className={`border-t ${agg === 'unreliable' ? 'opacity-60' : ''}`}>
                        <td className="px-3 py-2">{s.a} vs {s.b}</td>
                        <td className="px-3 py-2 text-gray-700">
                          {metricLabel(s.metric)}
                          {agg && agg !== 'reliable_absolute' && (
                            <span className={`ml-1 text-[10px] font-semibold ${RELIABILITY_META[agg].cls}`}
                              title={`Aggregate of the trajectory judge's axes — ${RELIABILITY_META[agg].word}; only as reliable as its weakest calibrated axis. See the Trajectory profile heatmap reliability gate.`}>
                              {RELIABILITY_META[agg].glyph}
                            </span>
                          )}
                          {/* SPA-88: a row is a claim about two configs made THROUGH an
                              axis, so how far that axis is trusted travels with it. */}
                          {s.axis?.kind === 'outcome_axis' && s.axis.status && s.axis.status !== 'reliable_absolute' && (
                            <span className={`ml-1 text-[10px] font-semibold ${RELIABILITY_META[s.axis.status].cls}`}
                              title={`Measured through the ${s.axis.name} axis — ${RELIABILITY_META[s.axis.status].word}. ${
                                s.axis.numeric
                                  ? 'Cleared for a comparison of means.'
                                  : s.axis.rank
                                    ? 'Ranks only — the difference in means this row reports is not supported by the calibration.'
                                    : 'Quarantined: the trusted view drops this row entirely.'
                              }`}>
                              {RELIABILITY_META[s.axis.status].glyph}
                            </span>
                          )}
                          {s.rank_only && (
                            <span className="ml-1 text-[10px] px-1 rounded bg-amber-50 text-amber-700 font-medium"
                              title="Rank-rescued axis: Welch compares MEANS, which a scale-shifted judge cannot support, so it was not run — the verdict rests on Mann-Whitney alone, which is approximate.">
                              ranks
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${judge.cls}`}>{judge.label}</span>
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap">
                          {s.design === 'unpaired' ? (
                            <span className="text-amber-700" title={
                              `Fewer than ${significanceCorrection?.min_cases ?? 3} cases run by both configs, so the comparison falls back to Welch over the cell values.`
                            }>unpaired</span>
                          ) : (
                            <span className="text-gray-600" title={
                              s.primary_test === 'sign'
                                ? 'Paired, on the signs of the within-case differences — the t-test had no variance to work with (or the rank test too few non-zero pairs). The design does not change because one inference is unavailable.'
                                : s.primary_test === 'identical'
                                  ? 'The two configs scored identically on every shared case. No paired test can run on that, and none is needed: see the equivalence verdict.'
                                  : 'Compared case by case, so everything the two configs share cancels instead of drowning the effect.'
                            }>paired n={s.n_pairs}</span>
                          )}
                          {!!(s.unpaired_cases && (s.unpaired_cases.a.length || s.unpaired_cases.b.length)) && (
                            <span className="ml-1 text-[10px] text-amber-600" title={
                              `Run by only one side, so absent from the pairing: ` +
                              [...(s.unpaired_cases.a.map((c) => `${s.a}: ${c}`)),
                               ...(s.unpaired_cases.b.map((c) => `${s.b}: ${c}`))].join(', ')
                            }>
                              −{s.unpaired_cases.a.length + s.unpaired_cases.b.length}
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap">
                          {/* SPA-115: withheld is not the same as missing. A
                              rank-rescued axis agrees with the human on order and
                              not on scale, so no size may be quoted from it. */}
                          {s.magnitudes_withheld ? (
                            <span className="text-amber-700 text-xs" title="This axis is trusted for ORDER only: a rescaling that preserves every rank would move a mean difference, its interval, the effect size and the equivalence verdict freely. None of them is reported here.">
                              withheld — ranks only
                            </span>
                          ) : s.ci ? (
                            <span title={
                              `${s.effect_kind === 'cohens_dz' ? "Cohen's d_z" : "Hedges' g"} = ${s.effect ?? '—'}. ` +
                              `95% bootstrap interval for the mean difference, resampling cases.`
                            }>
                              <span className="text-gray-700">{s.ci.mean_diff > 0 ? '+' : ''}{s.ci.mean_diff.toFixed(2)}</span>
                              <span className="text-gray-400"> [{s.ci.lo.toFixed(2)}, {s.ci.hi.toFixed(2)}]</span>
                            </span>
                          ) : <span className="text-gray-400">—</span>}
                        </td>
                        <td className="px-3 py-2 text-gray-500">{s.p.toFixed(4)}</td>
                        <td className="px-3 py-2">
                          {s.q === undefined ? <span className="text-gray-400">—</span> : (
                            <span className={s.significant ? 'text-gray-900 font-medium' : 'text-gray-500'}>
                              {s.q.toFixed(4)}
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap">
                          {s.significant ? (
                            <span className="text-green-700 font-medium">★ significant</span>
                          ) : s.equivalence && 'equivalent' in s.equivalence && s.equivalence.equivalent ? (
                            <span className="text-blue-700 font-medium" title={
                              `Both one-sided tests reject a difference larger than ±${s.equivalence.margin} judge points, ` +
                              `so «no difference» is a claim here rather than an absence of one.`
                            }>≈ equivalent</span>
                          ) : (
                            <span className="text-gray-400" title={
                              s.magnitudes_withheld
                                ? 'Could not tell. Equivalence is a claim about how small a difference is, in judge points — a rank-only axis cannot support one, so this row can never read «equivalent».'
                                : s.power?.n_required
                                  ? `Could not tell. This design could have detected a difference of ${s.power.mde.toFixed(2)}; ` +
                                    `the ${Math.abs(s.power.observed_diff).toFixed(2)} it saw would have needed n ≈ ${s.power.n_required} cases.`
                                  : 'Could not tell — and not enough data to call it equivalent either.'
                            }>? inconclusive</span>
                          )}
                          {/* A row that was green before the correction should not
                              simply stop being green with no trace. */}
                          {!s.significant && s.significant_uncorrected && (
                            <span className="ml-1 text-[10px] px-1 rounded bg-amber-50 text-amber-700 font-medium"
                              title="Nominally significant on its own (p < 0.05), but not after correcting for how many tests this family ran.">
                              was p&lt;.05
                            </span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
              </tbody>
            </table>
          </div>
        )}
      </section>

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
  const [includeRetired, setIncludeRetired] = useState(false)
  const [openTask, setOpenTask] = useState<string | null>(null)
  const hasRetired = detail.configurations.some((c) => c.retired_at)
  // Verifiable bench (executable checker = outcome ground truth): the outcome
  // judge (E-02) is the audited subject, not the eval — hide its score column. (SPA-68)
  const verifiable = detail.matrix.some((c) => (c.external_total ?? 0) > 0)
  const labelOf = new Map(detail.configurations.map((c) => [c.config_key, c.label]))
  const { data: rows = [] } = useQuery({
    queryKey: ['experiment-results', id, config, caseKey, includeRetired],
    queryFn: () =>
      experimentsApi.results(id, {
        ...(config ? { config } : {}),
        ...(caseKey ? { case: caseKey } : {}),
        ...(includeRetired ? { includeRetired: true } : {}),
      }),
  })
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <select value={config} onChange={(e) => setConfig(e.target.value)}
          className="px-2 py-1.5 border rounded text-sm bg-white">
          <option value="">all configurations</option>
          {detail.configurations.map((c) => (
            <option key={c.config_key} value={c.config_key}>
              {c.label}{c.retired_at ? ' (retired)' : ''}
            </option>
          ))}
        </select>
        <select value={caseKey} onChange={(e) => setCaseKey(e.target.value)}
          className="px-2 py-1.5 border rounded text-sm bg-white">
          <option value="">all cases</option>
          {detail.dataset_cases.map((c) => (
            <option key={c.case_key} value={c.case_key}>{c.case_key}</option>
          ))}
        </select>
        {hasRetired && (
          <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer"
            title="Cells of a retired configuration keep their lineage but are excluded from the report and every other view. This is the way back to them.">
            <input type="checkbox" checked={includeRetired}
              onChange={(e) => setIncludeRetired(e.target.checked)} />
            include retired
          </label>
        )}
        <span className="text-xs text-gray-400">{rows.length} runs</span>
      </div>
      <div className="bg-white border rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
            <tr>
              <th className="px-3 py-2"></th>
              <th className="px-3 py-2">Cell</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Verdict</th>
              {!verifiable && <th className="px-3 py-2">Quality</th>}
              <th className="px-3 py-2">Trajectory</th>
              <th className="px-3 py-2">Cost</th>
              <th className="px-3 py-2">Time</th>
              <th className="px-3 py-2">Result</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const canInspect = !!r.task_id
              const open = openTask === r.task_id
              return (
                <Fragment key={`${r.config_key}-${r.case_key}-${r.run_index}`}>
                  <tr className="border-t">
                    <td className="px-3 py-2">
                      {canInspect && (
                        <button
                          onClick={() => setOpenTask((t) => (t === r.task_id ? null : r.task_id!))}
                          className={`px-2.5 py-1 text-xs rounded border whitespace-nowrap transition-colors ${
                            open
                              ? 'border-blue-400 bg-blue-50 text-blue-700'
                              : 'border-gray-300 bg-white text-gray-600 hover:bg-blue-50 hover:border-blue-400 hover:text-blue-700'
                          }`}
                        >
                          {open ? 'close' : 'inspect'}
                        </button>
                      )}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap text-gray-700">
                      <span className={r.retired_at ? 'line-through text-gray-400' : ''}>
                        {labelOf.get(r.config_key) || r.config_key} · {r.case_key} · #{r.run_index + 1}
                      </span>
                      {/* SPA-84: >1 means this row is the survivor of earlier
                          attempts, which are preserved in the ledger. */}
                      {(r.attempt_count ?? 0) > 1 && (
                        <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-amber-100 text-amber-700 font-medium"
                          title={`Attempt ${r.attempt_count} — this cell was re-run. Earlier attempts are kept in the ledger and are visible with selection=all_attempts on the report.`}>
                          att {r.attempt_count}
                        </span>
                      )}
                      {r.retired_at && (
                        <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 font-medium"
                          title={`Configuration retired ${r.retired_at} — kept for lineage, excluded from the report.`}>
                          retired
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <span className={
                        r.status === 'success' ? 'text-green-600' :
                        r.status === 'failed' ? 'text-red-600' :
                        r.status === 'running' ? 'text-blue-600' : 'text-gray-400'
                      }>{r.status}</span>
                    </td>
                    <td className="px-3 py-2">
                      {r.external_verdict === 'pass' ? (
                        <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">pass</span>
                      ) : r.external_verdict === 'fail' ? (
                        <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">fail</span>
                      ) : (
                        <span className="text-gray-300">—</span>
                      )}
                    </td>
                    {!verifiable && <td className="px-3 py-2">{fmt(r.weighted_score, 1)}</td>}
                    <td className="px-3 py-2">{fmt(r.trajectory_score, 1)}</td>
                    <td className="px-3 py-2">${r.cost_usd.toFixed(3)}</td>
                    <td className="px-3 py-2">{r.duration_seconds != null ? `${r.duration_seconds}s` : '—'}</td>
                    <td className="px-3 py-2 text-gray-500 max-w-md truncate" title={r.result_summary || ''}>
                      {r.result_summary || '—'}
                    </td>
                  </tr>
                  {open && r.task_id && (
                    <tr className="border-t bg-gray-50">
                      <td colSpan={verifiable ? 8 : 9} className="px-3 py-3">
                        <div className="max-w-[68rem] min-w-0 sticky left-0">
                          <RunAnalysis
                            taskId={r.task_id}
                            profile={r.quality_profile ?? null}
                            verifiable={verifiable}
                            onSaved={() => setOpenTask(null)}
                          />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
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
  const [showClone, setShowClone] = useState(false)

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
  const retryFailedMutation = useMutation({ mutationFn: () => experimentsApi.retryFailed(id), onSuccess: invalidate })
  const deleteMutation = useMutation({
    mutationFn: () => experimentsApi.remove(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
      navigate('/experiments')
    },
  })

  const cloneMutation = useMutation({
    mutationFn: async (opts: { alsoRun?: boolean; name?: string; changes?: Record<string, unknown> }) => {
      const payload: { name?: string; changes?: Record<string, unknown> } = {}
      if (opts.name) payload.name = opts.name
      if (opts.changes && Object.keys(opts.changes).length) payload.changes = opts.changes
      const clone = await experimentsApi.clone(id, payload)
      if (opts.alsoRun) await experimentsApi.run(clone.id)
      return clone
    },
    onSuccess: (clone) => {
      setShowClone(false)
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
  const failedCount = (detail.matrix ?? []).reduce((s, c) => s + (c.counts?.failed ?? 0), 0)

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
            <RevisionBadge revision={detail.revision} retired={detail.n_retired_configs} />
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            {detail.n_configs} configs × {detail.n_cases} cases × {detail.n_runs_per_cell} runs = {detail.total_runs} ·
            spent ${detail.accumulated_cost_usd.toFixed(2)}
            {detail.budget_limit_usd != null && ` / $${detail.budget_limit_usd.toFixed(2)}`}
            {detail.description ? ` · ${detail.description}` : ''}
          </p>
        </div>
        <div className="flex flex-wrap justify-end gap-2">
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
          <button onClick={() => setShowClone(true)} title="Clone as a new draft, optionally tweaking name / runs / budget"
            className="flex items-center gap-1.5 px-3 py-2 border rounded-lg hover:bg-gray-50 text-sm">
            <Copy className="h-4 w-4" /> Clone…
          </button>
          {isTerminal && failedCount > 0 && (
            <button onClick={() => { if (confirm(`Re-run ${failedCount} failed cell(s) (rate-limit / API / infra errors)? Their current state is kept in the attempt ledger — nothing is overwritten. Valid cells and their scores are untouched.`)) retryFailedMutation.mutate() }}
              disabled={retryFailedMutation.isPending}
              title="Archive each failed cell's current state as an attempt, then reset it to pending and re-run it in THIS experiment — no clone, valid cells untouched. Bumps the experiment revision. Repeatable across provider quota windows."
              className="flex items-center gap-1.5 px-3 py-2 border border-amber-300 text-amber-700 rounded-lg hover:bg-amber-50 text-sm font-medium">
              <RotateCcw className="h-4 w-4" /> Retry failed ({failedCount})
            </button>
          )}
          {isTerminal && (
            <button onClick={() => cloneMutation.mutate({ alsoRun: true })} title="Full reproduction: clone + run"
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
          {detail.status !== 'running' && (
            <button onClick={() => { if (confirm('Delete this experiment? This cannot be undone.')) deleteMutation.mutate() }}
              title="Delete experiment"
              className="flex items-center gap-1.5 px-3 py-2 border border-red-200 text-red-600 rounded-lg hover:bg-red-50 text-sm">
              <Trash2 className="h-4 w-4" /> Delete
            </button>
          )}
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
      {tab === 'report' && <ReportTab id={id} isTerminal={isTerminal} detail={detail} />}
      {tab === 'runs' && <RunsTab id={id} detail={detail} filter={runsFilter} />}

      {showClone && (
        <CloneModal detail={detail} pending={cloneMutation.isPending}
          onClose={() => setShowClone(false)} onClone={(o) => cloneMutation.mutate(o)} />
      )}
    </div>
  )
}
