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
        title="The LLM judge has not been validated against human ratings yet."
      >
        <ShieldAlert className="h-3.5 w-3.5" />
        judge not calibrated
      </span>
    )
  }

  const kappa = data.overall_kappa
  const kappaText = kappa != null ? kappa.toFixed(2) : 'n/a'
  // People (SPA-85), not distinct account strings. Pre-ledger ratings carry no
  // attributable person, so they are named separately rather than inflating the
  // count the badge is read for.
  const humans = data.n_humans ?? 0
  const legacy = data.n_legacy ?? 0
  const inter = data.inter_annotator_kappa
  const title = [
    `Judge validated against ${humans} human annotator(s); overall verdict agreement κ=${kappaText}.`,
    legacy > 0 ? `${legacy} pre-ledger rating(s) are in the population but attributable to nobody.` : null,
    inter != null
      ? `Agreement between annotators on ${data.inter_annotator_records ?? 0} doubly-rated run(s): κ=${inter.toFixed(2)}.`
      : 'No run has been rated by a second annotator, so inter-annotator agreement is unknown.',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs',
        data.passed ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700',
        className,
      )}
      title={title}
    >
      <ShieldCheck className="h-3.5 w-3.5" />
      judge calibrated against {humans} humans, κ={kappaText}
      {legacy > 0 && <span className="opacity-70">+{legacy} legacy</span>}
    </span>
  )
}
