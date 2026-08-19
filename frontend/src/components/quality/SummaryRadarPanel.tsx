import { useState } from 'react'
import {
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  ResponsiveContainer,
  Legend,
  Tooltip,
} from 'recharts'

interface Row {
  config_key: string
  label: string
  cells: Record<string, { mean?: number | null } | undefined>
}

// SPA-88: the six-way trust taxonomy (see AxisTrustStatus in types). Only the
// grey/non-grey distinction matters here — a radar is a shape, not a claim.
type AxisStatus =
  | 'reliable_absolute'
  | 'moderate_agreement'
  | 'rank_only'
  | 'insufficient'
  | 'unreliable'
  | 'not_calibrated'

/** Overlayable per-config radar over a set of axes (quality dimensions E-02, or
 *  trajectory axes E-07). Each config is a semi-transparent layer; checkboxes
 *  toggle which configs are shown so they can be compared on one chart.
 *  When axisStatus is given (SPA-76), axis labels the process judge can't be
 *  trusted on are greyed (and struck + ⚠ when unreliable). */
export default function SummaryRadarPanel({
  title,
  subtitle,
  axes,
  axisLabel,
  axisStatus,
  rows,
  colorOf,
}: {
  title: string
  subtitle?: string
  axes: string[]
  axisLabel: (key: string) => string
  axisStatus?: (key: string) => AxisStatus | undefined
  rows: Row[]
  colorOf: (key: string) => string | undefined
}) {
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const toggle = (k: string) =>
    setHidden((prev) => {
      const next = new Set(prev)
      next.has(k) ? next.delete(k) : next.add(k)
      return next
    })

  if (axes.length < 3 || rows.length === 0) return null

  // One data row per axis; each config contributes a numeric key.
  const data = axes.map((ax) => {
    const point: Record<string, number | string | null> = { axis: axisLabel(ax) }
    for (const r of rows) point[r.config_key] = r.cells[ax]?.mean ?? null
    return point
  })
  const shown = rows.filter((r) => !hidden.has(r.config_key))

  // SPA-76: grey the axis label where the process judge is unverified, strike +
  // ⚠ where it is unreliable, so a below-the-bar axis can't quietly imply a win.
  const statusByLabel: Record<string, AxisStatus> = {}
  if (axisStatus) {
    for (const ax of axes) {
      const st = axisStatus(ax)
      if (st) statusByLabel[axisLabel(ax)] = st
    }
  }
  const renderTick = (props: {
    x?: number | string
    y?: number | string
    textAnchor?: string
    payload?: { value?: string | number }
  }) => {
    const value = String(props.payload?.value ?? '')
    const st = statusByLabel[value]
    const struck = st === 'unreliable'
    const fill =
      st === 'unreliable' || st === 'not_calibrated' || st === 'insufficient' ? '#9ca3af' : '#6b7280'
    return (
      <text
        x={props.x}
        y={props.y}
        textAnchor={props.textAnchor as 'start' | 'middle' | 'end' | 'inherit' | undefined}
        fontSize={11}
        fill={fill}
        style={struck ? { textDecoration: 'line-through' } : undefined}
      >
        {struck ? `${value} ⚠` : value}
      </text>
    )
  }

  return (
    <section>
      <h3 className="font-semibold text-gray-900 mb-1">
        {title}
        {subtitle && <span className="text-xs text-gray-400 font-normal"> {subtitle}</span>}
      </h3>
      <div className="bg-white border rounded-lg p-3">
        <div className="flex flex-wrap gap-x-4 gap-y-1 mb-2">
          {rows.map((r) => (
            <label key={r.config_key} className="flex items-center gap-1.5 text-xs cursor-pointer select-none">
              <input
                type="checkbox"
                checked={!hidden.has(r.config_key)}
                onChange={() => toggle(r.config_key)}
                className="accent-current"
                style={{ color: colorOf(r.config_key) }}
              />
              <span style={{ color: colorOf(r.config_key) }}>●</span>
              <span className="text-gray-700">{r.label}</span>
            </label>
          ))}
        </div>
        <div style={{ width: '100%', height: 340 }}>
          <ResponsiveContainer>
            <RadarChart data={data} outerRadius="70%">
              <PolarGrid />
              <PolarAngleAxis dataKey="axis" tick={axisStatus ? renderTick : { fontSize: 11, fill: '#6b7280' }} />
              <PolarRadiusAxis domain={[0, 10]} tick={{ fontSize: 10, fill: '#9ca3af' }} />
              {shown.map((r) => (
                <Radar
                  key={r.config_key}
                  name={r.label || r.config_key}
                  dataKey={r.config_key}
                  stroke={colorOf(r.config_key)}
                  fill={colorOf(r.config_key)}
                  fillOpacity={0.12}
                  strokeWidth={2}
                />
              ))}
              <Legend />
              <Tooltip />
            </RadarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </section>
  )
}
