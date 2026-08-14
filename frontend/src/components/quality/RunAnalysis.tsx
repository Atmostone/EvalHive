import { useState, type ReactNode } from 'react'
import type { QualityProfile } from '@/types'
import CleanedTracePanel from './CleanedTracePanel'
import OutcomeScorePanel from './OutcomeScorePanel'
import TrajectoryScorePanel from './TrajectoryScorePanel'
import EvidenceBankPanel from './EvidenceBankPanel'
import TrajectoryMatchPanel from './TrajectoryMatchPanel'
import CapabilityPanel from './CapabilityPanel'
import VarianceRunPanel from './VarianceRunPanel'
import PerturbationPanel from './PerturbationPanel'
import FailureModePanel from './FailureModePanel'
import HallucinationPanel from './HallucinationPanel'
import CalibrationPanel from './CalibrationPanel'
import AnnotationPanel from './AnnotationPanel'
import ExternalCheckerPanel from './ExternalCheckerPanel'

type Tab = 'trajectory' | 'robustness' | 'failure' | 'annotate'

const TABS: { key: Tab; label: string }[] = [
  { key: 'trajectory', label: 'Outcome & trajectory' },
  { key: 'robustness', label: 'Robustness' },
  { key: 'failure', label: 'Failure & facts' },
  { key: 'annotate', label: 'Annotate' },
]

/** Dim a panel that does not apply to checker-graded (verifiable) runs, with a
 *  one-line reason, while leaving it expandable. Pass-through when not dimmed. */
function Dimmed({ when, note, children }: { when: boolean; note: string; children: ReactNode }) {
  if (!when) return <>{children}</>
  return (
    <div className="opacity-55" title={note}>
      <div className="text-[11px] text-gray-400 mb-1">⊘ {note}</div>
      {children}
    </div>
  )
}

/** Per-run eval drill-down for an experiment run. Mounts the taskId-keyed
 *  quality panels — cleaned trace (E-06), trajectory (E-07), evidence bank
 *  (E-08), trajectory match (E-09), capability (E-13), variance (E-11),
 *  perturbation (E-12), failure modes (E-14), hallucination (E-15),
 *  calibration (E-16) — plus human feedback (E-05). These previously lived
 *  only in TaskDetail, which is unreachable for experiment-origin tasks; here
 *  they are keyed by the run's task_id (returned per run by the results API). */
export default function RunAnalysis({
  taskId,
  profile,
  verifiable = false,
  onSaved,
}: {
  taskId: string
  profile?: QualityProfile | null
  /** Checker-graded run: dim the evals that don't apply when an executable
   *  checker is the outcome ground truth (E-09/E-13/E-16). (SPA-68) */
  verifiable?: boolean
  onSaved?: () => void
}) {
  const [tab, setTab] = useState<Tab>('trajectory')
  return (
    <div className="space-y-3">
      <div className="flex gap-1 border-b">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-3 py-1.5 text-sm border-b-2 -mb-px ${
              tab === t.key
                ? 'border-blue-600 text-blue-600 font-medium'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="space-y-4">
        {tab === 'trajectory' && (
          <>
            <Dimmed when={verifiable} note="N/A for checker-graded tasks — the executable checker is the outcome ground truth (the outcome judge is off)">
              <OutcomeScorePanel profile={profile ?? null} />
            </Dimmed>
            <CleanedTracePanel taskId={taskId} />
            <TrajectoryScorePanel taskId={taskId} />
            <EvidenceBankPanel taskId={taskId} />
            <Dimmed when={verifiable} note="N/A for checker-graded tasks — no gold trajectory (the executable checker is the outcome truth)">
              <TrajectoryMatchPanel taskId={taskId} />
            </Dimmed>
          </>
        )}
        {tab === 'robustness' && (
          <>
            <Dimmed when={verifiable} note="N/A for this benchmark — no capability spec to isolate against">
              <CapabilityPanel taskId={taskId} />
            </Dimmed>
            <VarianceRunPanel taskId={taskId} />
            <PerturbationPanel taskId={taskId} />
          </>
        )}
        {tab === 'failure' && (
          <>
            {/* Why the executable checker passed/failed — the ground-truth oracle
                on verifiable benches (renders only there). */}
            <ExternalCheckerPanel taskId={taskId} verifiable={verifiable} />
            <FailureModePanel taskId={taskId} />
            <HallucinationPanel taskId={taskId} />
            <Dimmed when={verifiable} note="N/A for checker-graded tasks — calibration pairs with the outcome judge, which is off here">
              <CalibrationPanel taskId={taskId} />
            </Dimmed>
          </>
        )}
        {/* Always sighted: this drawer shows the judge's analysis in its other
            tabs, so a blind session here would be a claim we could not make. */}
        {tab === 'annotate' && (
          <AnnotationPanel taskId={taskId} verifiable={verifiable} onSaved={onSaved} />
        )}
      </div>
    </div>
  )
}
