import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart3 } from 'lucide-react'
import { analyticsApi } from '@/api/client'
import { cn } from '@/lib/utils'
import TemplateMetricsTable from '@/components/analytics/TemplateMetricsTable'
import TimelineChart from '@/components/analytics/TimelineChart'
import ModelChart from '@/components/analytics/ModelChart'
import TemplateCompareView from '@/components/analytics/TemplateCompareView'
import JudgeCalibrationPanel from '@/components/quality/JudgeCalibrationPanel'
import BiasReportPanel from '@/components/quality/BiasReportPanel'
import RankingPanel from '@/components/quality/RankingPanel'
import ReproducibilityPanel from '@/components/quality/ReproducibilityPanel'

type Period = 'day' | 'week' | 'month' | 'all'
type Tab = 'overview' | 'compare' | 'judge' | 'bias' | 'ranking' | 'repro'

// Tabs that render a self-contained panel with its own data (no analytics fetch).
const PANEL_TABS: Tab[] = ['judge', 'bias', 'ranking', 'repro']

const PERIODS: { value: Period; label: string; days: number }[] = [
  { value: 'day', label: 'Day', days: 1 },
  { value: 'week', label: 'Week', days: 7 },
  { value: 'month', label: 'Month', days: 30 },
  { value: 'all', label: 'All', days: 365 },
]

const STALE_TIME = 30_000

export default function Analytics() {
  const [period, setPeriod] = useState<Period>('week')
  const [tab, setTab] = useState<Tab>('overview')

  const days = PERIODS.find((p) => p.value === period)?.days ?? 7

  const templatesQuery = useQuery({
    queryKey: ['analytics', 'templates', period],
    queryFn: () => analyticsApi.templates({ period }),
    staleTime: STALE_TIME,
  })

  const timelineQuery = useQuery({
    queryKey: ['analytics', 'timeline', days],
    queryFn: () => analyticsApi.timeline({ days }),
    staleTime: STALE_TIME,
  })

  const modelsQuery = useQuery({
    queryKey: ['analytics', 'models', period],
    queryFn: () => analyticsApi.models({ period }),
    staleTime: STALE_TIME,
  })

  const templates = templatesQuery.data ?? []
  const timeline = timelineQuery.data ?? []
  const models = modelsQuery.data ?? []

  const isLoading = templatesQuery.isLoading || timelineQuery.isLoading || modelsQuery.isLoading
  const allEmpty =
    !isLoading && templates.length === 0 && timeline.length === 0 && models.length === 0

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
          <BarChart3 className="h-6 w-6" />
          Analytics
        </h1>
        <div className="flex items-center gap-1 bg-white border rounded-lg p-1">
          {PERIODS.map((p) => (
            <button
              key={p.value}
              onClick={() => setPeriod(p.value)}
              className={cn(
                'px-3 py-1 text-sm rounded-md transition-colors',
                period === p.value
                  ? 'bg-gray-900 text-white'
                  : 'text-gray-600 hover:bg-gray-100',
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="border-b">
        <div className="flex gap-4">
          <button
            onClick={() => setTab('overview')}
            className={cn(
              'px-1 py-2 -mb-px text-sm font-medium border-b-2 transition-colors',
              tab === 'overview'
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-500 hover:text-gray-700',
            )}
          >
            Overview
          </button>
          <button
            onClick={() => setTab('compare')}
            className={cn(
              'px-1 py-2 -mb-px text-sm font-medium border-b-2 transition-colors',
              tab === 'compare'
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-500 hover:text-gray-700',
            )}
          >
            A/B Compare
          </button>
          <button
            onClick={() => setTab('judge')}
            className={cn(
              'px-1 py-2 -mb-px text-sm font-medium border-b-2 transition-colors',
              tab === 'judge'
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-500 hover:text-gray-700',
            )}
          >
            Judge Calibration
          </button>
          <button
            onClick={() => setTab('bias')}
            className={cn(
              'px-1 py-2 -mb-px text-sm font-medium border-b-2 transition-colors',
              tab === 'bias'
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-500 hover:text-gray-700',
            )}
          >
            Bias Mitigation
          </button>
          <button
            onClick={() => setTab('ranking')}
            className={cn(
              'px-1 py-2 -mb-px text-sm font-medium border-b-2 transition-colors',
              tab === 'ranking'
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-500 hover:text-gray-700',
            )}
          >
            Leaderboard
          </button>
          <button
            onClick={() => setTab('repro')}
            className={cn(
              'px-1 py-2 -mb-px text-sm font-medium border-b-2 transition-colors',
              tab === 'repro'
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-500 hover:text-gray-700',
            )}
          >
            Reproducibility
          </button>
        </div>
      </div>

      {tab === 'judge' && <JudgeCalibrationPanel />}
      {tab === 'bias' && <BiasReportPanel />}
      {tab === 'ranking' && <RankingPanel />}
      {tab === 'repro' && <ReproducibilityPanel />}

      {!PANEL_TABS.includes(tab) && isLoading && (
        <div className="bg-white rounded-lg border p-8 text-center text-gray-500">
          Loading analytics…
        </div>
      )}

      {!PANEL_TABS.includes(tab) && !isLoading && allEmpty && (
        <div className="bg-white rounded-lg border p-12 text-center text-gray-500">
          <BarChart3 className="h-12 w-12 mx-auto mb-3 text-gray-300" />
          <p className="text-base">No data yet — complete some tasks first</p>
        </div>
      )}

      {!isLoading && !allEmpty && tab === 'overview' && (
        <div className="space-y-6">
          <section>
            <h2 className="text-lg font-semibold mb-3">Templates</h2>
            <TemplateMetricsTable data={templates} />
          </section>
          <section>
            <h2 className="text-lg font-semibold mb-3">Timeline</h2>
            <TimelineChart data={timeline} />
          </section>
          <section>
            <h2 className="text-lg font-semibold mb-3">Models</h2>
            <ModelChart data={models} />
          </section>
        </div>
      )}

      {!isLoading && !allEmpty && tab === 'compare' && <TemplateCompareView data={templates} />}
    </div>
  )
}
