import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, EyeOff, Users } from 'lucide-react'
import { format } from 'date-fns'
import { qualityApi } from '@/api/client'
import HumanFeedbackForm from './HumanFeedbackForm'
import MarkdownView from '../MarkdownView'
import type { Annotation, AnnotatorType, ReviewFile } from '@/types'

/** Opens an annotation session and shows everything it was served: what is being
 *  rated, the existing ledger, and the rating form. Used by the calibration
 *  queue and the experiment results drawer.
 *
 *  One session call rather than five reads is the point (SPA-85): the protocol is
 *  declared before anything is fetched, the server builds the whole bundle to
 *  match it in one place, and the submitted rating records the protocol from the
 *  session id — so a blind session cannot be half-applied. */
export default function AnnotationPanel({
  taskId,
  verifiable = false,
  blind: blindProp = false,
  onSaved,
}: {
  taskId: string
  /** Verifiable bench (executable checker = outcome ground truth): surface a
   *  top-level "rate the process only" banner so the annotator knows there is no
   *  human outcome rating here. (SPA-74) */
  verifiable?: boolean
  /** Blind protocol (SPA-85). Captured once, on mount — the choice has to precede
   *  the fetch. It only *declares* the protocol; what the rating records comes
   *  from the session the server opened, so this prop cannot manufacture a blind
   *  annotation. */
  blind?: boolean
  onSaved?: () => void
}) {
  const [blind] = useState(blindProp)
  const sessionQuery = useQuery({
    queryKey: ['annotation-session', taskId, blind],
    queryFn: () => qualityApi.startAnnotationSession(taskId, blind),
    // A session is single-use and stamps a row, so it must not be replayed from
    // cache or refetched behind the annotator's back.
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
  })

  if (sessionQuery.isLoading) return <div className="text-xs text-gray-400 py-2">Loading…</div>
  if (!sessionQuery.data) {
    return (
      <div className="text-xs text-red-600 py-2">
        Could not open an annotation session — rating is disabled rather than recorded
        under an unknown protocol.
      </div>
    )
  }

  const bundle = sessionQuery.data
  const profile = bundle.quality_profile
  const review = bundle.review

  return (
    <div className="space-y-3">
      {verifiable && (
        <div className="text-xs text-blue-700 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">
          Verifiable bench — the executable checker is the outcome ground truth (the outcome judge is off). Rate the{' '}
          <span className="font-medium">process (trajectory)</span> only; there's no human outcome rating here.
        </div>
      )}
      {bundle.protocol.blind_to_judge && (
        <div className="flex items-start gap-2 text-xs text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-lg px-3 py-2">
          <EyeOff className="h-4 w-4 shrink-0 mt-px" />
          <span>
            <span className="font-medium">Blind session.</span> The judge's scores and the
            model name were not sent to this page, and your rating will record that. It
            says what this session was served — not that you have never seen the judge
            elsewhere.
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
        trajectoryProfile={bundle.trajectory_profile}
        existing={bundle.human_feedback}
        blind={bundle.protocol.blind_to_judge}
        sessionId={bundle.session_id}
        defaultOpen
        onSaved={onSaved}
      />

      <AnnotationLedger rows={bundle.annotations} />
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
