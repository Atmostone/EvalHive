import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, CheckCircle2, XCircle, ShieldCheck, AlertTriangle } from 'lucide-react'
import { qualityApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { BiasReport, BiasDimensionDelta, BiasReportMetrics } from '@/types'
import { cn } from '@/lib/utils'

/** Bias Mitigation Toolkit (E-18): a controlled A/B re-judge of the calibration set
 *  with the prompt-level mitigations OFF vs ON. Shows per-dimension agreement-with-
 *  human before/after, the overall delta, and per-bias diagnostics (verbosity,
 *  score-clustering, self-preference; position bias is deferred to pairwise / E-21).
 *  Unlike judge calibration this DOES spend LLM calls. Workspace-level — Analytics. */
export default function BiasReportPanel() {
  const queryClient = useQueryClient()
  const role = useAuth((s) => s.workspaces.find((w) => w.id === s.workspaceId)?.role ?? null)
  const isAdmin = role === 'owner' || role === 'admin'

  const history = useQuery({
    queryKey: ['bias-report', 'history'],
    queryFn: () => qualityApi.getBiasReport({ history: true }),
    retry: false,
  })

  const run = useMutation({
    mutationFn: () => qualityApi.runBiasReport(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['bias-report'] }),
  })

  const data = history.data as
    | { latest: BiasReport | null; history: BiasReport[] }
    | null
    | undefined
  const latest = data?.latest ?? null
  const versions = data?.history ?? []

  return (
    <div className="bg-white rounded-lg border p-4 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <ShieldCheck className="h-5 w-5" />
          Bias Mitigation <span className="text-sm font-normal text-gray-400">(E-18)</span>
        </h2>
        {isAdmin && (
          <button
            onClick={() => run.mutate()}
            disabled={run.isPending}
            className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
          >
            <RefreshCw className={cn('h-4 w-4', run.isPending && 'animate-spin')} />
            {run.isPending ? 'Re-judging…' : 'Run A/B report'}
          </button>
        )}
      </div>

      <p className="text-xs text-gray-500">
        Re-judges every calibration-set task with the prompt-level mitigations OFF then ON
        and compares agreement-with-human — so you can see whether mitigation actually moves
        the judge toward human ratings. <em>Spends LLM calls.</em>
      </p>

      {run.isError && (
        <p className="text-xs text-red-600">
          Run failed — the bias report requires owner/admin role.
        </p>
      )}

      {history.isFetching && <p className="text-sm text-gray-400">Loading…</p>}

      {!history.isFetching && !latest && (
        <p className="text-sm text-gray-400">
          No bias report yet. Collect human feedback on some tasks, then run the A/B report.
        </p>
      )}

      {latest && <ReportView report={latest} />}

      {versions.length > 1 && <VersionHistory versions={versions} />}
    </div>
  )
}

function ReportView({ report }: { report: BiasReport }) {
  const m = report.metrics
  if (m.status !== 'ok') {
    return (
      <p className="text-sm text-amber-700">
        {m.status === 'empty'
          ? 'No tasks with human feedback to re-judge yet.'
          : m.status === 'no_judge_model'
            ? 'No judge model configured for this workspace.'
            : 'Not enough rated tasks to compute a report.'}
      </p>
    )
  }
  const od = m.overall_delta
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
        <span className="text-gray-700">{report.judge_config_key}</span>
        <span>v{report.version}</span>
        <span>
          {m.sample_size} pairs · {m.n_records} records
        </span>
        <span>threshold κ ≥ {m.threshold_kappa}</span>
        {report.created_at && <span>{new Date(report.created_at).toLocaleString()}</span>}
      </div>

      <DeltaTable dimensions={m.dimensions_delta} />

      {od && (
        <div className="flex items-center gap-3 text-xs border-t pt-2">
          <span className="font-medium text-gray-700">Overall verdict agreement</span>
          {od.improved ? (
            <span className="flex items-center gap-1 text-green-700">
              <CheckCircle2 className="h-4 w-4" /> improved
            </span>
          ) : (
            <span className="flex items-center gap-1 text-amber-700">
              <XCircle className="h-4 w-4" /> no improvement
            </span>
          )}
          <span className="ml-auto text-gray-500 tabular-nums">
            κ {fmt(od.cohen_kappa_before)} → {fmt(od.cohen_kappa_after)} · agreement{' '}
            {pct(od.agreement_pct_before)} → {pct(od.agreement_pct_after)}
          </span>
        </div>
      )}

      <Diagnostics m={m} />
    </div>
  )
}

function DeltaTable({ dimensions }: { dimensions: BiasDimensionDelta[] }) {
  if (dimensions.length === 0) {
    return <p className="text-xs text-gray-400">No rated dimensions yet.</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-gray-400 border-b">
            <th className="py-1 pr-2 font-medium">Dimension</th>
            <th className="py-1 px-2 font-medium text-right">κ before</th>
            <th className="py-1 px-2 font-medium text-right">κ after</th>
            <th className="py-1 px-2 font-medium text-right">r before</th>
            <th className="py-1 px-2 font-medium text-right">r after</th>
            <th className="py-1 pl-2 font-medium text-center">improved</th>
          </tr>
        </thead>
        <tbody>
          {dimensions.map((d) => (
            <tr key={d.key} className="border-b last:border-0">
              <td className="py-1 pr-2">{d.name}</td>
              <td className="py-1 px-2 text-right tabular-nums">{fmt(d.cohen_kappa_before)}</td>
              <td className="py-1 px-2 text-right tabular-nums">{fmt(d.cohen_kappa_after)}</td>
              <td className="py-1 px-2 text-right tabular-nums">{fmt(d.pearson_before)}</td>
              <td className="py-1 px-2 text-right tabular-nums">{fmt(d.pearson_after)}</td>
              <td className="py-1 pl-2 text-center">
                {d.improved ? (
                  <CheckCircle2 className="h-4 w-4 text-green-600 inline" />
                ) : (
                  <XCircle className="h-4 w-4 text-gray-300 inline" />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Diagnostics({ m }: { m: BiasReportMetrics }) {
  const d = m.diagnostics
  return (
    <div className="border-t pt-2 space-y-1.5 text-xs">
      <DiagRow
        label="Verbosity"
        improved={d.verbosity.improved}
        status={d.verbosity.status}
        detail={`length↔score r ${fmt(d.verbosity.judge_corr_off)} → ${fmt(
          d.verbosity.judge_corr_on,
        )} (human ${fmt(d.verbosity.human_corr)})`}
      />
      <DiagRow
        label="Score clustering"
        improved={d.score_clustering.improved}
        status={d.score_clustering.status}
        detail={`spread ${fmt(d.score_clustering.spread_off)} → ${fmt(
          d.score_clustering.spread_on,
        )}`}
      />
      <div className="flex items-start gap-2">
        <span className="font-medium text-gray-700 w-28 shrink-0">Self-preference</span>
        {d.self_preference.flagged ? (
          <span className="flex items-start gap-1 text-amber-700">
            <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
            {d.self_preference.warning}
          </span>
        ) : (
          <span className="text-gray-500">judge ≠ agent model — no self-preference risk</span>
        )}
      </div>
      <div className="flex items-center gap-2 text-gray-400">
        <span className="font-medium w-28 shrink-0">Position</span>
        <span>n/a — {d.position_bias.reason}</span>
      </div>
    </div>
  )
}

function DiagRow({
  label,
  improved,
  status,
  detail,
}: {
  label: string
  improved?: boolean
  status: string
  detail: string
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="font-medium text-gray-700 w-28 shrink-0">{label}</span>
      {status !== 'ok' ? (
        <span className="text-gray-400">insufficient data</span>
      ) : improved ? (
        <span className="flex items-center gap-1 text-green-700">
          <CheckCircle2 className="h-3.5 w-3.5" /> improved
        </span>
      ) : (
        <span className="flex items-center gap-1 text-gray-500">
          <XCircle className="h-3.5 w-3.5" /> no change
        </span>
      )}
      <span className="ml-auto text-gray-500 tabular-nums">{detail}</span>
    </div>
  )
}

function VersionHistory({ versions }: { versions: BiasReport[] }) {
  return (
    <div className="border-t pt-2 space-y-1">
      <h5 className="text-xs font-medium text-gray-600">History</h5>
      {versions.map((v) => (
        <div key={v.id} className="flex items-center justify-between text-xs text-gray-500">
          <span className="text-gray-700">
            v{v.version} · {v.judge_config_key}
          </span>
          <span className="tabular-nums">
            n={v.sample_size} · {v.passed ? 'improved' : 'no improvement'}
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

function pct(value: number | null | undefined): string {
  return value == null ? '—' : `${Math.round(value * 100)}%`
}
