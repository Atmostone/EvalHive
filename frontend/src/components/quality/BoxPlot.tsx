import type { VarianceDistribution } from '@/types'

/** Minimal box-plot (recharts has no native one). Renders, per dimension, a
 *  min–p25–median–p75–max box with whiskers and the individual run samples as
 *  dots, scaled to a per-row domain. */

export interface BoxPlotDatum {
  label: string
  unit?: string
  dist: VarianceDistribution
  /** Fixed domain (e.g. [0,10] for scores); defaults to the sample min/max. */
  domain?: [number, number]
}

function pct(v: number, lo: number, hi: number): number {
  if (hi <= lo) return 50
  return ((v - lo) / (hi - lo)) * 100
}

function Row({ d }: { d: BoxPlotDatum }) {
  const { dist } = d
  if (!dist || dist.n === 0) {
    return (
      <div className="text-xs text-gray-400">
        {d.label}: <span className="italic">no samples</span>
      </div>
    )
  }
  const lo = d.domain ? d.domain[0] : (dist.min ?? 0)
  const hi = d.domain ? d.domain[1] : (dist.max ?? 1)
  const p25 = dist.p25 ?? dist.min ?? 0
  const p50 = dist.p50 ?? 0
  const p75 = dist.p75 ?? dist.max ?? 0
  const mn = dist.min ?? 0
  const mx = dist.max ?? 0

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-gray-700">
          {d.label}
          {d.unit ? <span className="text-gray-400"> ({d.unit})</span> : null}
        </span>
        <span className="text-gray-500 tabular-nums">
          μ {dist.mean} · σ {dist.std} · n={dist.n}
        </span>
      </div>
      <svg width="100%" height="40" className="overflow-visible">
        {/* whisker */}
        <line x1={`${pct(mn, lo, hi)}%`} x2={`${pct(mx, lo, hi)}%`} y1="20" y2="20"
          stroke="#9ca3af" strokeWidth="1" />
        <line x1={`${pct(mn, lo, hi)}%`} x2={`${pct(mn, lo, hi)}%`} y1="12" y2="28"
          stroke="#9ca3af" strokeWidth="1" />
        <line x1={`${pct(mx, lo, hi)}%`} x2={`${pct(mx, lo, hi)}%`} y1="12" y2="28"
          stroke="#9ca3af" strokeWidth="1" />
        {/* IQR box */}
        <rect x={`${pct(p25, lo, hi)}%`} y="8" width={`${Math.max(0, pct(p75, lo, hi) - pct(p25, lo, hi))}%`}
          height="24" fill="#c7d2fe" stroke="#6366f1" strokeWidth="1" rx="2" />
        {/* median */}
        <line x1={`${pct(p50, lo, hi)}%`} x2={`${pct(p50, lo, hi)}%`} y1="8" y2="32"
          stroke="#4338ca" strokeWidth="2" />
        {/* individual samples */}
        {dist.values.map((v, i) => (
          <circle key={i} cx={`${pct(v, lo, hi)}%`} cy="20" r="2.5"
            fill="#312e81" fillOpacity="0.55" />
        ))}
      </svg>
      <div className="flex justify-between text-[10px] text-gray-400 tabular-nums">
        <span>{Math.round(lo * 100) / 100}</span>
        <span>p25 {p25} · p50 {p50} · p75 {p75}</span>
        <span>{Math.round(hi * 100) / 100}</span>
      </div>
    </div>
  )
}

export default function BoxPlot({ data }: { data: BoxPlotDatum[] }) {
  return (
    <div className="space-y-3">
      {data.map((d) => (
        <Row key={d.label} d={d} />
      ))}
    </div>
  )
}
