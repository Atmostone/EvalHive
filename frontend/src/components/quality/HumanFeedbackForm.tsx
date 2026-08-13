import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { qualityApi } from '@/api/client'
import { MessageSquarePlus, Check, Eye, EyeOff } from 'lucide-react'
import type { QualityProfile, TrajectoryProfile, HumanFeedback, FeedbackBand, FeedbackVerdict } from '@/types'
import { cn } from '@/lib/utils'

/** Human feedback collection (E-05): rate the E-02 axes 1-10, comment, submit.
 *  Optional and non-blocking; stored as a parallel signal next to the judge profile. */

const BAND_BAD_MAX = 3
const BAND_IMPROVE_MAX = 7

function band(score: number): FeedbackBand {
  if (score <= BAND_BAD_MAX) return 'bad'
  if (score <= BAND_IMPROVE_MAX) return 'improve'
  return 'good'
}

const BAND_STYLE: Record<FeedbackBand, string> = {
  bad: 'text-red-600',
  improve: 'text-amber-600',
  good: 'text-green-600',
}
const BAND_LABEL: Record<FeedbackBand, string> = {
  bad: 'incorrect',
  improve: 'improve',
  good: 'correct',
}
const ACCENT: Record<FeedbackBand, string> = {
  bad: 'accent-red-500',
  improve: 'accent-amber-500',
  good: 'accent-green-500',
}

// When the judge produced no score for a dimension there is nothing to calibrate
// against; explain WHY so the annotator can decide whether to add an independent
// rating rather than being handed a pre-filled (fabricated) value.
function judgeNote(judgeScore: number | null, status?: string): string | null {
  if (judgeScore != null) return null
  switch (status) {
    case 'deferred': return 'judge deferred'
    case 'skipped': return 'judge skipped'
    case 'error': return 'judge errored'
    case 'not_applicable': return 'not applicable'
    default: return 'judge did not score'
  }
}

interface Props {
  taskId: string
  profile: QualityProfile | null
  trajectoryProfile?: TrajectoryProfile | null
  existing: HumanFeedback | null
  /** Start expanded (e.g. the calibration queue, where the form is the whole point). */
  defaultOpen?: boolean
  /** Called after a successful submit, for callers that track annotation progress. */
  onSaved?: () => void
}

export default function HumanFeedbackForm({ taskId, profile, trajectoryProfile, existing, defaultOpen = false, onSaved }: Props) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(defaultOpen)

  // Dimensions to rate: the quality (E-02) axes + the process/trajectory (E-07)
  // axes the judges evaluated. Keys never collide across the two. ``judgeScore`` is
  // null when the judge produced no score for a dimension (deferred / skipped /
  // errored / not-applicable) — those carry a ``status`` for the badge.
  const qDims = (profile?.dimensions ?? []).map((d) => ({
    key: d.key, name: d.name, judgeScore: d.score ?? null, status: d.status as string | undefined,
  }))
  const tDims = (trajectoryProfile?.axes ?? []).map((a) => ({
    key: a.key, name: a.name, judgeScore: a.score ?? null,
    status: (a as { status?: string }).status,
  }))
  const groups = [
    { label: 'Quality · outcome', dims: qDims },
    { label: 'Process · trajectory', dims: tDims },
  ].filter((g) => g.dims.length > 0)
  const dims = [...qDims, ...tDims]
  // Verifiable run (executable checker = outcome ground truth): the outcome judge
  // is skipped (no quality dims) but the trajectory judge ran. Don't show/solicit
  // an outcome rating — only the process is human-rateable. (SPA-68)
  const verifiable = qDims.length === 0 && tDims.length > 0
  // Seed from existing human feedback, else the judge's score (one-click
  // agreement). CRUCIALLY: when the judge produced NO score, seed null (unrated) —
  // never a fabricated 5. A human-5 paired against a judge-null dimension pollutes
  // the human-feedback aggregate and manufactures a calibration pair the judge
  // never earned (E-17). The human must explicitly opt in to rate such a dimension.
  const seedScore = (key: string, judge: number | null): number | null => {
    const prev = existing?.dimensions.find((d) => d.key === key)
    if (prev) return prev.score
    return judge
  }
  const [scores, setScores] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(dims.map((d) => [d.key, seedScore(d.key, d.judgeScore)])),
  )
  const [comments, setComments] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      dims.map((d) => [d.key, existing?.dimensions.find((x) => x.key === d.key)?.comment ?? '']),
    ),
  )
  const [overall, setOverall] = useState(existing?.overall_comment ?? '')
  // Blind protocol (SPA-85). Off by default, because that is what the form has
  // always done — it shows the judge's score and seeds from it, so an annotator
  // anchors on the number they are supposed to be independent of. Turning it on
  // hides the judge here AND clears every score, so the rating starts from
  // nothing; the submitted annotation records which of the two happened.
  const [blind, setBlind] = useState(false)
  const toggleBlind = () => {
    const next = !blind
    setBlind(next)
    if (next) setScores(Object.fromEntries(dims.map((d) => [d.key, null])))
  }
  // Overall approve/reject verdict — the human's binary judgment of the run. This is
  // the signal the checker↔human agreement (and the human heatmap verdict column)
  // are built from; without it that calibration has no human side.
  const [verdict, setVerdict] = useState<FeedbackVerdict | null>(existing?.verdict ?? null)

  const mutation = useMutation({
    mutationFn: () =>
      qualityApi.saveFeedback(taskId, {
        verdict,
        overall_comment: overall.trim() || null,
        // Only dimensions the human actually rated — never the unrated (null) ones,
        // so we don't fabricate a score the annotator didn't give (calibration hygiene).
        dimensions: dims
          .filter((d) => scores[d.key] != null)
          .map((d) => ({
            key: d.key,
            name: d.name,
            score: scores[d.key] as number,
            comment: comments[d.key]?.trim() || null,
          })),
        // Recorded as it actually was, not as intended. The form can only
        // vouch for the judge's scores — the model identity is shown elsewhere
        // on the page, so it is never claimed here.
        blind_to_judge: blind,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['human-feedback', taskId] })
      queryClient.invalidateQueries({ queryKey: ['annotations', taskId] })
      setOpen(false)
      onSaved?.()
    },
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <MessageSquarePlus className="h-4 w-4" />
        {existing ? 'Edit your feedback' : 'Give feedback'}
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Your feedback</h4>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={toggleBlind}
            title={
              blind
                ? 'Blind: the judge’s scores are hidden and nothing was seeded from them. Recorded on the annotation.'
                : 'Rate without seeing the judge’s scores. Turning this on clears every score, so your rating cannot start from the judge’s.'
            }
            className={cn(
              'flex items-center gap-1 text-xs px-2 py-1 rounded border transition-colors',
              blind
                ? 'bg-indigo-600 text-white border-indigo-600'
                : 'bg-white text-gray-500 border-gray-300 hover:bg-gray-50',
            )}
          >
            {blind ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
            blind to judge
          </button>
          <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
            cancel
          </button>
        </div>
      </div>

      {blind && (
        <p className="text-xs text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-lg px-3 py-2">
          Blind protocol — judge scores are hidden here and none were used to seed your
          ratings. The judge's side is still frozen onto your annotation for calibration;
          the model identity may remain visible elsewhere on this page.
        </p>
      )}

      {verifiable && (
        <p className="text-xs text-gray-500 bg-white border rounded-lg px-3 py-2">
          Outcome is verified by an executable checker (ground truth) — no judge or human
          result rating here. Rate the process only.
        </p>
      )}

      {dims.length === 0 ? (
        <p className="text-xs text-gray-400">Evaluate quality or trajectory first to rate dimensions.</p>
      ) : (
        <div className="space-y-4">
          {groups.map((g) => (
            <div key={g.label} className="space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">{g.label}</div>
              {g.dims.map((d) => {
            const score = scores[d.key]
            const rated = score != null
            const b = rated ? band(score) : null
            const note = blind ? null : judgeNote(d.judgeScore, d.status)
            return (
              <div key={d.key} className="space-y-1">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-700 flex items-center gap-2">
                    {d.name}
                    {note && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-gray-200 text-gray-600"
                        title="The judge produced no score for this dimension, so there is nothing to calibrate against. Rate it only if you want to add an independent human score."
                      >
                        {note}
                      </span>
                    )}
                  </span>
                  <span className="flex items-center gap-2">
                    {rated ? (
                      <span className={cn('font-medium', BAND_STYLE[b!])}>
                        {score}/10 · {BAND_LABEL[b!]}
                      </span>
                    ) : (
                      <span className="text-xs text-gray-400">not rated</span>
                    )}
                    {!blind && d.judgeScore != null && (
                      <button
                        type="button"
                        onClick={() => setScores((s) => ({ ...s, [d.key]: d.judgeScore as number }))}
                        title="Agree with the judge's score"
                        className="text-xs text-gray-400 hover:text-blue-600 flex items-center gap-0.5"
                      >
                        <Check className="h-3 w-3" /> judge {d.judgeScore}
                      </button>
                    )}
                  </span>
                </div>
                {rated ? (
                  <>
                    <input
                      type="range"
                      min={0}
                      max={10}
                      step={1}
                      value={score}
                      onChange={(e) => setScores((s) => ({ ...s, [d.key]: Number(e.target.value) }))}
                      className={cn('w-full', ACCENT[b!])}
                    />
                    <div className="flex items-center gap-2">
                      <input
                        type="text"
                        value={comments[d.key]}
                        onChange={(e) => setComments((c) => ({ ...c, [d.key]: e.target.value }))}
                        placeholder="comment (optional)"
                        className="flex-1 px-2 py-1 border rounded text-xs bg-white"
                      />
                      {(blind || d.judgeScore == null) && (
                        <button
                          type="button"
                          onClick={() => setScores((s) => ({ ...s, [d.key]: null }))}
                          title="Clear your rating — leave this dimension unrated"
                          className="text-[11px] text-gray-400 hover:text-red-600 whitespace-nowrap"
                        >
                          clear
                        </button>
                      )}
                    </div>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => setScores((s) => ({ ...s, [d.key]: blind ? 5 : d.judgeScore ?? 5 }))}
                    className="text-xs px-2 py-1 border border-dashed rounded text-gray-500 hover:text-blue-600 hover:border-blue-400"
                  >
                    + rate this dimension
                  </button>
                )}
              </div>
            )
              })}
            </div>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-medium text-gray-600">Verdict:</span>
        <button
          onClick={() => setVerdict(verdict === 'approve' ? null : 'approve')}
          className={cn(
            'px-3 py-1 rounded-lg text-sm border transition-colors',
            verdict === 'approve' ? 'bg-green-600 text-white border-green-600' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50',
          )}
        >
          ✓ Approve
        </button>
        <button
          onClick={() => setVerdict(verdict === 'reject' ? null : 'reject')}
          className={cn(
            'px-3 py-1 rounded-lg text-sm border transition-colors',
            verdict === 'reject' ? 'bg-red-600 text-white border-red-600' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50',
          )}
        >
          ✗ Reject
        </button>
        {verdict && (
          <button onClick={() => setVerdict(null)} className="text-xs text-gray-400 hover:underline">clear</button>
        )}
        <span className="text-[11px] text-gray-400">
          {verifiable ? 'feeds checker↔human agreement' : 'feeds judge↔human / checker↔human agreement'}
        </span>
      </div>

      <textarea
        value={overall}
        onChange={(e) => setOverall(e.target.value)}
        placeholder="Overall comment (optional)"
        className="w-full p-2 border rounded-lg text-sm resize-none h-16 bg-white"
      />

      <button
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm disabled:opacity-50"
      >
        {mutation.isPending ? 'Saving…' : 'Submit feedback'}
      </button>
    </div>
  )
}
