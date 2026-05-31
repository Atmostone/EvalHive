import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, CheckCircle2, XCircle, Scale } from 'lucide-react'
import { qualityApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { JudgeCalibration, JudgeCalibrationDimension } from '@/types'
import { cn } from '@/lib/utils'
import JudgeCalibrationBadge from './JudgeCalibrationBadge'

/** Judge Calibration Protocol (E-17): validates the LLM judge (E-02) against human
 *  feedback (E-05) purely from stored scores. Shows per-dimension agreement
 *  (Pearson / Spearman / Cohen's κ on bands) and an overall verdict-agreement, with
 *  the version history. Workspace-level — lives on the Analytics page. */
export default function JudgeCalibrationPanel() {
  const queryClient = useQueryClient()
  const role = useAuth((s) => s.workspaces.find((w) => w.id === s.workspaceId)?.role ?? null)
  const isAdmin = role === 'owner' || role === 'admin'

  const history = useQuery({
    queryKey: ['judge-calibration', 'history'],
    queryFn: () => qualityApi.getJudgeCalibration({ history: true }),
    retry: false,
  })

  const run = useMutation({
    mutationFn: () => qualityApi.runJudgeCalibration(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['judge-calibration'] })
      queryClient.invalidateQueries({ queryKey: ['judge-calibration-badge'] })
    },
  })

  const data = history.data as
    | { latest: JudgeCalibration | null; history: JudgeCalibration[] }
    | null
    | undefined
  const latest = data?.latest ?? null
  const versions = data?.history ?? []

  return (
    <div className="bg-white rounded-lg border p-4 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <Scale className="h-5 w-5" />
          Judge Calibration <span className="text-sm font-normal text-gray-400">(E-17)</span>
        </h2>
        <div className="flex items-center gap-3">
          <JudgeCalibrationBadge />
          {isAdmin && (
            <button
              onClick={() => run.mutate()}
              disabled={run.isPending}
              className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              <RefreshCw className={cn('h-4 w-4', run.isPending && 'animate-spin')} />
              {run.isPending ? 'Computing…' : 'Run calibration'}
            </button>
          )}
        </div>
      </div>

      <p className="text-xs text-gray-500">
        Validates the LLM judge against human ratings on the same dimensions — no LLM
        call, pure agreement statistics over stored scores. A dimension is{' '}
        <em>reliable</em> when its band agreement (Cohen&apos;s κ) clears the threshold.
      </p>

      {run.isError && (
        <p className="text-xs text-red-600">
          Run failed — calibration requires owner/admin role.
        </p>
      )}

      {history.isFetching && <p className="text-sm text-gray-400">Loading…</p>}

      {!history.isFetching && !latest && (
        <p className="text-sm text-gray-400">
          Not calibrated yet. Collect human feedback on some tasks, then run calibration.
        </p>
      )}

      {latest && <ReportView report={latest} />}

      {versions.length > 1 && <VersionHistory versions={versions} />}
    </div>
  )
}

function ReportView({ report }: { report: JudgeCalibration }) {
  const m = report.metrics
  const overall = m.overall
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
        <span className="text-gray-700">{report.judge_config_key}</span>
        <span>v{report.version}</span>
        <span>
          {m.sample_size} pairs · {m.n_records} records · {m.n_humans} humans
        </span>
        <span>threshold κ ≥ {report.threshold_kappa}</span>
        {report.created_at && <span>{new Date(report.created_at).toLocaleString()}</span>}
      </div>

      <DimensionTable dimensions={m.dimensions} />

      <div className="flex items-center gap-3 text-xs border-t pt-2">
        <span className="font-medium text-gray-700">Overall verdict agreement</span>
        {overall.reliable ? (
          <span className="flex items-center gap-1 text-green-700">
            <CheckCircle2 className="h-4 w-4" /> reliable
          </span>
        ) : (
          <span className="flex items-center gap-1 text-amber-700">
            <XCircle className="h-4 w-4" /> not reliable
          </span>
        )}
        <span className="ml-auto text-gray-500 tabular-nums">
          κ={fmt(overall.cohen_kappa)} · agreement{' '}
          {overall.agreement_pct != null ? `${Math.round(overall.agreement_pct * 100)}%` : '—'} ·
          n={overall.n}
        </span>
      </div>

      {m.recommendations.length > 0 && (
        <ul className="space-y-1 border-t pt-2">
          {m.recommendations.map((r, i) => (
            <li key={i} className="text-xs text-gray-600">
              {r}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function DimensionTable({ dimensions }: { dimensions: JudgeCalibrationDimension[] }) {
  if (dimensions.length === 0) {
    return <p className="text-xs text-gray-400">No rated dimensions yet.</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-gray-400 border-b">
            <th className="py-1 pr-2 font-medium">Dimension</th>
            <th className="py-1 px-2 font-medium text-right">n</th>
            <th className="py-1 px-2 font-medium text-right">Pearson r</th>
            <th className="py-1 px-2 font-medium text-right">Spearman ρ</th>
            <th className="py-1 px-2 font-medium text-right">Cohen κ</th>
            <th className="py-1 px-2 font-medium text-right">bias</th>
            <th className="py-1 pl-2 font-medium text-center">reliable</th>
          </tr>
        </thead>
        <tbody>
          {dimensions.map((d) => {
            const insufficient = d.status === 'insufficient_data'
            return (
              <tr
                key={d.key}
                className={cn('border-b last:border-0', insufficient && 'text-gray-400')}
              >
                <td className="py-1 pr-2">{d.name}</td>
                <td className="py-1 px-2 text-right tabular-nums">{d.n}</td>
                <td className="py-1 px-2 text-right tabular-nums">{fmt(d.pearson)}</td>
                <td className="py-1 px-2 text-right tabular-nums">{fmt(d.spearman)}</td>
                <td className="py-1 px-2 text-right tabular-nums">{fmt(d.cohen_kappa)}</td>
                <td className="py-1 px-2 text-right tabular-nums">{fmt(d.mean_bias)}</td>
                <td className="py-1 pl-2 text-center">
                  {insufficient ? (
                    <span title="insufficient data">—</span>
                  ) : d.reliable ? (
                    <CheckCircle2 className="h-4 w-4 text-green-600 inline" />
                  ) : (
                    <XCircle className="h-4 w-4 text-red-500 inline" />
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function VersionHistory({ versions }: { versions: JudgeCalibration[] }) {
  return (
    <div className="border-t pt-2 space-y-1">
      <h5 className="text-xs font-medium text-gray-600">History</h5>
      {versions.map((v) => (
        <div key={v.id} className="flex items-center justify-between text-xs text-gray-500">
          <span className="text-gray-700">
            v{v.version} · {v.judge_config_key}
          </span>
          <span className="tabular-nums">
            n={v.sample_size} · κ={fmt(v.metrics.overall.cohen_kappa)} ·{' '}
            {v.passed ? 'passed' : 'failed'}
            {v.created_at ? ` · ${new Date(v.created_at).toLocaleDateString()}` : ''}
          </span>
        </div>
      ))}
    </div>
  )
}

function fmt(value: number | null | undefined): string {
  return value == null ? '—' : value.toFixed(2)
}
