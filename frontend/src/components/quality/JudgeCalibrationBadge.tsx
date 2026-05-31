import { useQuery } from '@tanstack/react-query'
import { ShieldCheck, ShieldAlert } from 'lucide-react'
import { qualityApi } from '@/api/client'
import { cn } from '@/lib/utils'

/** Judge Calibration Protocol (E-17): a compact trust badge —
 *  "judge calibrated against N humans, κ=X.X" — backed by the latest calibration
 *  report. Renders "not calibrated" until the first report exists. */
export default function JudgeCalibrationBadge({ className }: { className?: string }) {
  const { data } = useQuery({
    queryKey: ['judge-calibration-badge'],
    queryFn: () => qualityApi.getJudgeCalibrationBadge(),
    retry: false,
  })

  if (!data) return null

  if (!data.calibrated) {
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500',
          className,
        )}
        title="The LLM judge has not been validated against human ratings yet (E-17)."
      >
        <ShieldAlert className="h-3.5 w-3.5" />
        judge not calibrated
      </span>
    )
  }

  const kappa = data.overall_kappa
  const kappaText = kappa != null ? kappa.toFixed(2) : 'n/a'
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs',
        data.passed ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700',
        className,
      )}
      title={`Judge validated against ${data.n_humans ?? 0} human rater(s); overall verdict agreement κ=${kappaText} (E-17).`}
    >
      <ShieldCheck className="h-3.5 w-3.5" />
      judge calibrated against {data.n_humans ?? 0} humans, κ={kappaText}
    </span>
  )
}
