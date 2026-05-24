import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { qualityApi } from '@/api/client'
import { MessageSquarePlus, Check } from 'lucide-react'
import type { QualityProfile, HumanFeedback, FeedbackBand } from '@/types'
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

interface Props {
  taskId: string
  profile: QualityProfile | null
  existing: HumanFeedback | null
}

export default function HumanFeedbackForm({ taskId, profile, existing }: Props) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  // Dimensions to rate: the rubric axes the judge evaluated. Seed from existing
  // human feedback, else default to the judge's score (one-click agreement).
  const dims = profile?.dimensions ?? []
  const seedScore = (key: string, judge: number | null) => {
    const prev = existing?.dimensions.find((d) => d.key === key)
    if (prev) return prev.score
    return judge ?? 5
  }
  const [scores, setScores] = useState<Record<string, number>>(() =>
    Object.fromEntries(dims.map((d) => [d.key, seedScore(d.key, d.score)])),
  )
  const [comments, setComments] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      dims.map((d) => [d.key, existing?.dimensions.find((x) => x.key === d.key)?.comment ?? '']),
    ),
  )
  const [overall, setOverall] = useState(existing?.overall_comment ?? '')

  const mutation = useMutation({
    mutationFn: () =>
      qualityApi.saveFeedback(taskId, {
        overall_comment: overall.trim() || null,
        dimensions: dims.map((d) => ({
          key: d.key,
          name: d.name,
          score: scores[d.key],
          comment: comments[d.key]?.trim() || null,
        })),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['human-feedback', taskId] })
      setOpen(false)
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
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          cancel
        </button>
      </div>

      {dims.length === 0 ? (
        <p className="text-xs text-gray-400">Evaluate quality first to rate dimensions.</p>
      ) : (
        <div className="space-y-3">
          {dims.map((d) => {
            const score = scores[d.key]
            const b = band(score)
            return (
              <div key={d.key} className="space-y-1">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-700">{d.name}</span>
                  <span className="flex items-center gap-2">
                    <span className={cn('font-medium', BAND_STYLE[b])}>
                      {score}/10 · {BAND_LABEL[b]}
                    </span>
                    {d.score != null && (
                      <button
                        type="button"
                        onClick={() => setScores((s) => ({ ...s, [d.key]: d.score as number }))}
                        title="Agree with the judge's score"
                        className="text-xs text-gray-400 hover:text-blue-600 flex items-center gap-0.5"
                      >
                        <Check className="h-3 w-3" /> judge {d.score}
                      </button>
                    )}
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={10}
                  step={1}
                  value={score}
                  onChange={(e) => setScores((s) => ({ ...s, [d.key]: Number(e.target.value) }))}
                  className={cn('w-full', ACCENT[b])}
                />
                <input
                  type="text"
                  value={comments[d.key]}
                  onChange={(e) => setComments((c) => ({ ...c, [d.key]: e.target.value }))}
                  placeholder="comment (optional)"
                  className="w-full px-2 py-1 border rounded text-xs bg-white"
                />
              </div>
            )
          })}
        </div>
      )}

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
