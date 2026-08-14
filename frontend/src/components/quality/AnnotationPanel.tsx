import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, EyeOff, Users } from 'lucide-react'
import { format } from 'date-fns'
import { qualityApi } from '@/api/client'
import HumanFeedbackForm from './HumanFeedbackForm'
import MarkdownView from '../MarkdownView'
import type { Annotation, AnnotatorType, QualityProfile, ReviewFile } from '@/types'

/** Loads the judge profile (unless supplied), the review context (task prompt +
 *  deliverable) and any existing human feedback for a task, then shows what is
 *  being rated above the rating form. Used by the calibration queue and the
 *  experiment results drawer so both annotate through the one feedback API. */
export default function AnnotationPanel({
  taskId,
  profile: profileProp,
  verifiable = false,
  blind: blindProp = false,
  onSaved,
}: {
  taskId: string
  profile?: QualityProfile | null
  /** Verifiable bench (executable checker = outcome ground truth): surface a
   *  top-level "rate the process only" banner so the annotator knows there is no
   *  human outcome rating here. (SPA-74) */
  verifiable?: boolean
  /** Blind protocol (SPA-85). Captured once, on mount: the choice has to precede
   *  the fetch, and flipping it afterwards would mean the annotator had already
   *  been shown what they claim not to have seen. The server enforces the rest —
   *  it strips the judge's scores and derives the stored flag from what it
   *  actually served, so this prop cannot manufacture a blind annotation. */
  blind?: boolean
  onSaved?: () => void
}) {
  const [blind] = useState(blindProp)
  const profileQuery = useQuery({
    queryKey: ['quality-profile', taskId, blind],
    queryFn: () => qualityApi.getProfile(taskId, blind),
    enabled: profileProp == null,
  })
  const reviewQuery = useQuery({
    queryKey: ['review-context', taskId],
    queryFn: () => qualityApi.getReview(taskId),
  })
  const feedbackQuery = useQuery({
    queryKey: ['human-feedback', taskId, blind],
    queryFn: () => qualityApi.getFeedback(taskId, blind),
  })
  const trajectoryQuery = useQuery({
    queryKey: ['trajectory-profile', taskId, blind],
    queryFn: () => qualityApi.getTrajectoryProfile(taskId, blind),
  })
  const annotationsQuery = useQuery({
    queryKey: ['annotations', taskId, blind],
    queryFn: () => qualityApi.getAnnotations(taskId, blind),
  })

  const profile = profileProp ?? profileQuery.data?.quality_profile ?? null
  const loading =
    feedbackQuery.isLoading ||
    reviewQuery.isLoading ||
    trajectoryQuery.isLoading ||
    (profileProp == null && profileQuery.isLoading)
  if (loading) return <div className="text-xs text-gray-400 py-2">Loading…</div>

  const review = reviewQuery.data

  return (
    <div className="space-y-3">
      {verifiable && (
        <div className="text-xs text-blue-700 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">
          Verifiable bench — the executable checker is the outcome ground truth (the outcome judge is off). Rate the{' '}
          <span className="font-medium">process (trajectory)</span> only; there's no human outcome rating here.
        </div>
      )}
      {blind && (
        <div className="flex items-start gap-2 text-xs text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-lg px-3 py-2">
          <EyeOff className="h-4 w-4 shrink-0 mt-px" />
          <span>
            <span className="font-medium">Blind protocol.</span> The judge's scores were never
            sent to this page, so your rating is independent of them. Leaving blind mode
            reveals them — and any rating you make afterwards is recorded as sighted.
          </span>
        </div>
      )}
      {review && (
        <div className="space-y-3 text-sm">
          {review.description && (
            <Section label="Task">
              <p className="whitespace-pre-wrap text-gray-700">{review.description}</p>
            </Section>
          )}
          {review.reference_answer && (
            <Section label="Reference answer">
              <p className="whitespace-pre-wrap text-gray-700">{review.reference_answer}</p>
            </Section>
          )}
          <Section label="Result">
            {review.result_summary ? (
              <p className="whitespace-pre-wrap text-gray-700">{review.result_summary}</p>
            ) : (
              <p className="text-gray-400 italic">(no result summary)</p>
            )}
          </Section>
          {review.files.map((f) => (
            <ReviewFileCard key={f.name} taskId={taskId} file={f} />
          ))}
        </div>
      )}

      <HumanFeedbackForm
        taskId={taskId}
        profile={profile}
        trajectoryProfile={trajectoryQuery.data?.trajectory_profile ?? null}
        existing={feedbackQuery.data?.human_feedback ?? null}
        blind={blind}
        defaultOpen
        onSaved={onSaved}
      />

      <AnnotationLedger rows={annotationsQuery.data?.annotations ?? []} />
    </div>
  )
}

const TYPE_STYLE: Record<AnnotatorType, string> = {
  human: 'bg-green-100 text-green-700',
  llm_judge: 'bg-purple-100 text-purple-700',
  synthetic: 'bg-purple-100 text-purple-700',
  legacy: 'bg-gray-200 text-gray-600',
}

/** Every rating this run has ever carried (SPA-85). A second annotator is a
 *  second row here — that is what makes inter-annotator agreement computable —
 *  and a re-rating supersedes only its own author's previous row. */
function AnnotationLedger({ rows }: { rows: Annotation[] }) {
  if (rows.length === 0) return null
  const superseded = new Set(rows.map((r) => r.supersedes_id).filter(Boolean) as string[])
  const current = rows.filter((r) => !superseded.has(r.id))
  const people = new Set(current.filter((r) => r.annotator_type === 'human').map((r) => r.annotator_id))

  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-1 flex items-center gap-1">
        <Users className="h-3 w-3" />
        Annotations · {current.length} current
        {rows.length > current.length && ` (${rows.length - current.length} superseded)`}
        {people.size > 1 && ` · ${people.size} people`}
      </div>
      <div className="border rounded-lg bg-white divide-y">
        {rows.map((r) => {
          const stale = superseded.has(r.id)
          return (
            <div
              key={r.id}
              className={`px-3 py-1.5 flex items-center gap-2 text-xs ${stale ? 'opacity-50' : ''}`}
            >
              <span className={`px-1.5 py-0.5 rounded ${TYPE_STYLE[r.annotator_type]}`}>
                {r.annotator_type}
              </span>
              <span className="text-gray-700 truncate flex-1">{r.annotator_label ?? '—'}</span>
              {r.blind_to_judge && (
                <span
                  className="flex items-center gap-0.5 text-indigo-600"
                  title="Rated without seeing the judge's scores"
                >
                  <EyeOff className="h-3 w-3" /> blind
                </span>
              )}
              {r.verdict && (
                <span className={r.verdict === 'approve' ? 'text-green-600' : 'text-red-600'}>
                  {r.verdict}
                </span>
              )}
              <span className="text-gray-400">{r.dimensions.length} dims</span>
              {stale && <span className="text-gray-400 italic">superseded</span>}
              <span className="text-gray-400 whitespace-nowrap">
                {r.created_at ? format(new Date(r.created_at), 'dd MMM HH:mm') : ''}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-1">{label}</div>
      <div className="border rounded-lg bg-white px-3 py-2 max-h-72 overflow-auto">{children}</div>
    </div>
  )
}

/** One deliverable file: a header with the name + a "download original" link
 *  (always shown — the point of SPA-71 is the human can grab the binary too) and
 *  a body that prefers converted Markdown, falls back to a raw-text excerpt, then
 *  to a placeholder. ``file.name`` is the path after results/<task_id>/ and may
 *  contain slashes — pass it verbatim (the route accepts a path), don't strip. */
function ReviewFileCard({ taskId, file }: { taskId: string; file: ReviewFile }) {
  const href = encodeURI(`/api/tasks/${taskId}/files/${file.name}`)
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div className="text-xs font-medium uppercase tracking-wide text-gray-400">
          File · {file.name}
        </div>
        <a
          href={href}
          download
          className="flex items-center gap-1 text-xs text-blue-600 hover:underline"
        >
          <Download className="h-3 w-3" />
          original
        </a>
      </div>
      <div className="border rounded-lg bg-white px-3 py-2 max-h-72 overflow-auto">
        {file.markdown ? (
          <MarkdownView>{file.markdown}</MarkdownView>
        ) : file.text ? (
          <pre className="whitespace-pre-wrap text-xs text-gray-700">{file.text}</pre>
        ) : (
          <p className="text-gray-400 italic">(binary or unavailable — download to view)</p>
        )}
      </div>
    </div>
  )
}
