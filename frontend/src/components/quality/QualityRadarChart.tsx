import {
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  ResponsiveContainer,
  Tooltip,
} from 'recharts'
import { ShieldCheck, ShieldAlert } from 'lucide-react'
import type { QualityProfile } from '@/types'
import { cn } from '@/lib/utils'

/** Radar chart of a task's quality profile (E-02). Plots scored dimensions only. */
export default function QualityRadarChart({ profile }: { profile: QualityProfile }) {
  const scored = profile.dimensions.filter((d) => d.status === 'scored' && d.score != null)
  const deferred = profile.dimensions.filter((d) => d.status === 'deferred')
  const failed = profile.dimensions.filter((d) => d.status === 'error')
  const skipped = profile.dimensions.filter((d) => d.status === 'skipped')

  const dimLabel = (d: QualityProfile['dimensions'][number]) =>
    d.evaluator === 'reference' && d.reference_mode
      ? `${d.name} (${d.reference_mode})`
      : d.evaluator === 'objective' && d.probe
        ? `${d.name} (${d.probe})`
        : d.name

  const data = scored.map((d) => ({
    dimension: dimLabel(d),
    score: d.score as number,
    threshold: d.threshold ?? 0,
  }))

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-gray-500">
          Quality Profile{profile.rubric_name ? ` · ${profile.rubric_name}` : ''}
        </h3>
        <span
          className={cn(
            'flex items-center gap-1 text-xs px-2 py-0.5 rounded-full',
            profile.gate.passed ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700',
          )}
          title={
            profile.gate.passed
              ? 'All critical dimensions meet their thresholds'
              : `Below threshold: ${profile.gate.failed_dimensions.join(', ')}`
          }
        >
          {profile.gate.passed ? <ShieldCheck className="h-3 w-3" /> : <ShieldAlert className="h-3 w-3" />}
          {profile.gate.passed ? 'Gate passed' : 'Gate failed'}
        </span>
      </div>

      {data.length >= 3 ? (
        <div style={{ width: '100%', height: 260 }}>
          <ResponsiveContainer>
            <RadarChart data={data} outerRadius="70%">
              <PolarGrid />
              <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 11, fill: '#6b7280' }} />
              <PolarRadiusAxis domain={[0, 10]} tick={{ fontSize: 10, fill: '#9ca3af' }} />
              <Radar name="Score" dataKey="score" stroke="#2563eb" fill="#3b82f6" fillOpacity={0.5} />
              <Tooltip />
            </RadarChart>
          </ResponsiveContainer>
        </div>
      ) : data.length > 0 ? (
        <div className="space-y-1">
          {scored.map((d) => (
            <div key={d.key} className="flex items-center justify-between text-sm">
              <span className="text-gray-700">
                {d.name}
                {d.evaluator === 'reference' && d.reference_mode && (
                  <span className="text-gray-400"> ({d.reference_mode})</span>
                )}
                {d.evaluator === 'objective' && d.probe && (
                  <span className="text-gray-400"> ({d.probe})</span>
                )}
              </span>
              <span className="font-medium">{d.score}/10</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-xs text-gray-400">No scored dimensions.</p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
        {profile.weighted_score != null && (
          <span>
            Weighted: <span className="font-medium text-gray-700">{profile.weighted_score}/10</span>
          </span>
        )}
        <span>Judge: {profile.judge_model}</span>
        <span>${profile.judge_cost_usd.toFixed(4)}</span>
        {deferred.length > 0 && <span className="text-gray-400">{deferred.length} deferred</span>}
        {skipped.length > 0 && (
          <span className="text-gray-400" title="dimensions skipped — no reference answer, or no matching artifact for the probe">
            {skipped.length} skipped
          </span>
        )}
        {failed.length > 0 && <span className="text-red-500">{failed.length} failed</span>}
      </div>
    </div>
  )
}
