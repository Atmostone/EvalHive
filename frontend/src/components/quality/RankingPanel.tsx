import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Trophy } from 'lucide-react'
import { qualityApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { RankingReport, RankingPlayer } from '@/types'
import { cn } from '@/lib/utils'

type Subject = 'model' | 'template'
type Method = 'bt' | 'elo'

/** Aggregation Engine (E-19): a Bradley-Terry / Elo leaderboard built from pairwise
 *  matches. Until the pairwise framework (E-21) lands, matches are derived from the
 *  stored pointwise scores (same benchmark case, higher score wins), so this ranks
 *  real models/templates today. Shows each player's rating with a bootstrap 95% CI
 *  bar plus win/loss/tie tallies. No LLM calls. Workspace-level — Analytics. */
export default function RankingPanel() {
  const queryClient = useQueryClient()
  const role = useAuth((s) => s.workspaces.find((w) => w.id === s.workspaceId)?.role ?? null)
  const isAdmin = role === 'owner' || role === 'admin'

  const [subject, setSubject] = useState<Subject>('model')
  const [method, setMethod] = useState<Method>('bt')
  const rankingKey = `${subject}:${method}`

  const history = useQuery({
    queryKey: ['ranking', rankingKey, 'history'],
    queryFn: () => qualityApi.getRanking({ ranking_key: rankingKey, history: true }),
    retry: false,
  })

  const run = useMutation({
    mutationFn: () => qualityApi.runRanking({ subject, method }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['ranking', rankingKey] }),
  })

  const data = history.data as
    | { latest: RankingReport | null; history: RankingReport[] }
    | null
    | undefined
  const latest = data?.latest ?? null
  const versions = data?.history ?? []

  return (
    <div className="bg-white rounded-lg border p-4 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <Trophy className="h-5 w-5" />
          Leaderboard <span className="text-sm font-normal text-gray-400">(E-19)</span>
        </h2>
        <div className="flex items-center gap-2">
          <Segmented
            value={subject}
            onChange={(v) => setSubject(v as Subject)}
            options={[
              { value: 'model', label: 'Models' },
              { value: 'template', label: 'Templates' },
            ]}
          />
          <Segmented
            value={method}
            onChange={(v) => setMethod(v as Method)}
            options={[
              { value: 'bt', label: 'Bradley-Terry' },
              { value: 'elo', label: 'Elo' },
            ]}
          />
          {isAdmin && (
            <button
              onClick={() => run.mutate()}
              disabled={run.isPending}
              className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              <RefreshCw className={cn('h-4 w-4', run.isPending && 'animate-spin')} />
              {run.isPending ? 'Ranking…' : 'Run'}
            </button>
          )}
        </div>
      </div>

      <p className="text-xs text-gray-500">
        Ranks {subject === 'model' ? 'models' : 'templates'} from head-to-head matches via{' '}
        {method === 'bt' ? 'Bradley-Terry' : 'Elo'} with bootstrap confidence intervals. Matches are
        derived from stored pointwise scores — true pairwise judging arrives with E-21.
      </p>

      {run.isError && (
        <p className="text-xs text-red-600">Run failed — ranking requires owner/admin role.</p>
      )}

      {history.isFetching && <p className="text-sm text-gray-400">Loading…</p>}

      {!history.isFetching && !latest && (
        <p className="text-sm text-gray-400">
          No leaderboard yet for {rankingKey}. Run some tasks across shared benchmark cases, then
          Run the ranking.
        </p>
      )}

      {latest && <ReportView report={latest} />}

      {versions.length > 1 && <VersionHistory versions={versions} />}
    </div>
  )
}

function ReportView({ report }: { report: RankingReport }) {
  const m = report.metrics
  if (m.status !== 'ok') {
    return (
      <p className="text-sm text-amber-700">
        {m.status === 'empty'
          ? 'No matches to rank yet — need at least two competitors sharing a benchmark case.'
          : 'Not enough players to build a ranking (need at least two).'}
      </p>
    )
  }
  const lo = Math.min(...m.players.map((p) => p.ci_low))
  const hi = Math.max(...m.players.map((p) => p.ci_high))
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
        <span className="text-gray-700">{report.ranking_key}</span>
        <span>v{report.version}</span>
        <span>
          {m.n_players} players · {m.n_matches} matches
        </span>
        <span>source: {m.source}</span>
        {m.derivation && <span>{m.derivation.n_cases} cases</span>}
        {report.created_at && <span>{new Date(report.created_at).toLocaleString()}</span>}
      </div>

      <Leaderboard players={m.players} lo={lo} hi={hi} />
    </div>
  )
}

function Leaderboard({ players, lo, hi }: { players: RankingPlayer[]; lo: number; hi: number }) {
  const span = hi - lo || 1
  const pos = (v: number) => `${((v - lo) / span) * 100}%`
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-gray-400 border-b">
            <th className="py-1 pr-2 font-medium text-right">#</th>
            <th className="py-1 px-2 font-medium">Player</th>
            <th className="py-1 px-2 font-medium text-right">Rating</th>
            <th className="py-1 px-2 font-medium w-40">95% CI</th>
            <th className="py-1 px-2 font-medium text-right">W/L/T</th>
            <th className="py-1 pl-2 font-medium text-right">Win%</th>
          </tr>
        </thead>
        <tbody>
          {players.map((p) => (
            <tr key={p.player} className="border-b last:border-0">
              <td className="py-1 pr-2 text-right tabular-nums text-gray-400">{p.rank}</td>
              <td className="py-1 px-2 font-medium text-gray-700">{p.player}</td>
              <td className="py-1 px-2 text-right tabular-nums">{Math.round(p.rating)}</td>
              <td className="py-1 px-2">
                <div className="relative h-3 bg-gray-100 rounded">
                  <div
                    className="absolute h-3 bg-blue-200 rounded"
                    style={{ left: pos(p.ci_low), right: `calc(100% - ${pos(p.ci_high)})` }}
                    title={`${Math.round(p.ci_low)} – ${Math.round(p.ci_high)}`}
                  />
                  <div
                    className="absolute top-[-1px] h-3.5 w-0.5 bg-blue-700"
                    style={{ left: pos(p.rating) }}
                  />
                </div>
              </td>
              <td className="py-1 px-2 text-right tabular-nums text-gray-500">
                {p.wins}/{p.losses}/{p.ties}
              </td>
              <td className="py-1 pl-2 text-right tabular-nums">
                {p.win_rate == null ? '—' : `${Math.round(p.win_rate * 100)}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function VersionHistory({ versions }: { versions: RankingReport[] }) {
  return (
    <div className="border-t pt-2 space-y-1">
      <h5 className="text-xs font-medium text-gray-600">History</h5>
      {versions.map((v) => (
        <div key={v.id} className="flex items-center justify-between text-xs text-gray-500">
          <span className="text-gray-700">
            v{v.version} · {v.ranking_key}
          </span>
          <span className="tabular-nums">
            {v.n_players} players · {v.n_matches} matches
            {v.created_at ? ` · ${new Date(v.created_at).toLocaleDateString()}` : ''}
          </span>
        </div>
      ))}
    </div>
  )
}

function Segmented({
  value,
  onChange,
  options,
}: {
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <div className="inline-flex rounded-lg border overflow-hidden text-xs">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'px-2.5 py-1.5 hover:bg-gray-50',
            value === o.value ? 'bg-gray-100 font-medium text-gray-800' : 'text-gray-500',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}
