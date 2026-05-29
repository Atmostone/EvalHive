import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { tasksApi, eventsApi, qualityApi } from '@/api/client'
import { X, Check, RotateCcw, Clock, Play, Download, Gauge } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import type { Task } from '@/types'
import { PRIORITY_COLORS, TASK_STATUS_LABELS, SOURCE_COLORS } from '@/types'
import { cn } from '@/lib/utils'
import ReasoningTimeline from './ReasoningTimeline'
import AgentLogViewer from './AgentLogViewer'
import QualityRadarChart from '@/components/quality/QualityRadarChart'
import HumanFeedbackForm from '@/components/quality/HumanFeedbackForm'
import CleanedTracePanel from '@/components/quality/CleanedTracePanel'
import TrajectoryScorePanel from '@/components/quality/TrajectoryScorePanel'
import EvidenceBankPanel from '@/components/quality/EvidenceBankPanel'
import TrajectoryMatchPanel from '@/components/quality/TrajectoryMatchPanel'
import CapabilityPanel from '@/components/quality/CapabilityPanel'
import VarianceRunPanel from '@/components/quality/VarianceRunPanel'
import PerturbationPanel from '@/components/quality/PerturbationPanel'
import FailureModePanel from '@/components/quality/FailureModePanel'
import HallucinationPanel from '@/components/quality/HallucinationPanel'
import CalibrationPanel from '@/components/quality/CalibrationPanel'

interface TaskDetailProps {
  task: Task
  onClose: () => void
}

export default function TaskDetail({ task, onClose }: TaskDetailProps) {
  const queryClient = useQueryClient()
  const [rejectFeedback, setRejectFeedback] = useState('')
  const [showReject, setShowReject] = useState(false)

  const { data: detail } = useQuery({
    queryKey: ['task', task.id],
    queryFn: () => tasksApi.get(task.id),
    refetchInterval: 5000,
  })

  const { data: events = [] } = useQuery({
    queryKey: ['events', task.id],
    queryFn: () => eventsApi.list({ task_id: task.id, limit: 20 }),
    refetchInterval: 5000,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['tasks'] })
    queryClient.invalidateQueries({ queryKey: ['task', task.id] })
    queryClient.invalidateQueries({ queryKey: ['events', task.id] })
  }

  const startMutation = useMutation({
    mutationFn: () => tasksApi.update(task.id, { status: 'ready' }),
    onSuccess: invalidate,
  })

  const approveMutation = useMutation({
    mutationFn: () => tasksApi.approve(task.id),
    onSuccess: invalidate,
  })

  const rejectMutation = useMutation({
    mutationFn: () => tasksApi.reject(task.id, rejectFeedback),
    onSuccess: () => { setShowReject(false); setRejectFeedback(''); invalidate() },
  })

  const t = detail || task
  const isTerminal = ['done', 'failed', 'awaiting_approval'].includes(t.status)

  const { data: profileData } = useQuery({
    queryKey: ['quality-profile', task.id],
    queryFn: () => qualityApi.getProfile(task.id),
    enabled: isTerminal,
    retry: false,
  })
  const profile = profileData?.quality_profile ?? null

  const evaluateMutation = useMutation({
    mutationFn: () => qualityApi.evaluate(task.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['quality-profile', task.id] }),
  })

  const { data: feedbackData } = useQuery({
    queryKey: ['human-feedback', task.id],
    queryFn: () => qualityApi.getFeedback(task.id),
    enabled: isTerminal,
    retry: false,
  })
  const humanFeedback = feedbackData?.human_feedback ?? null

  return (
    <div className="fixed inset-y-0 right-0 w-[480px] bg-white shadow-xl border-l z-50 flex flex-col">
      <div className="flex items-center justify-between p-4 border-b">
        <h2 className="font-semibold text-lg truncate">{t.title}</h2>
        <button onClick={onClose} className="p-1 rounded hover:bg-gray-100">
          <X className="h-5 w-5" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Status & Priority */}
        <div className="flex gap-2">
          <span className="text-xs px-2 py-1 rounded-full bg-gray-100 text-gray-700">
            {TASK_STATUS_LABELS[t.status as keyof typeof TASK_STATUS_LABELS] || t.status}
          </span>
          <span className={cn('text-xs px-2 py-1 rounded-full', PRIORITY_COLORS[t.priority as keyof typeof PRIORITY_COLORS])}>
            {t.priority}
          </span>
        </div>

        {/* Description */}
        {t.description && (
          <div>
            <h3 className="text-sm font-medium text-gray-500 mb-1">Description</h3>
            <p className="text-sm text-gray-700 whitespace-pre-wrap">{t.description}</p>
          </div>
        )}

        {/* Reference answer (E-03) */}
        {t.reference_answer && (
          <div>
            <h3 className="text-sm font-medium text-gray-500 mb-1">Reference answer</h3>
            <p className="text-sm text-gray-700 whitespace-pre-wrap bg-amber-50 p-3 rounded-lg">
              {t.reference_answer}
            </p>
          </div>
        )}

        {/* Result */}
        {t.result_summary && (
          <div>
            <h3 className="text-sm font-medium text-gray-500 mb-1">Result</h3>
            <p className="text-sm text-gray-700 whitespace-pre-wrap bg-green-50 p-3 rounded-lg">
              {t.result_summary}
            </p>
          </div>
        )}

        {/* Files */}
        {t.result_files && t.result_files.length > 0 && (
          <div>
            <div className="flex items-center justify-between mb-1">
              <h3 className="text-sm font-medium text-gray-500">Files</h3>
              {t.result_files.length > 1 && (
                <a
                  href={`/api/tasks/${t.id}/files.zip`}
                  download
                  className="flex items-center gap-1 text-xs text-blue-600 hover:underline"
                >
                  <Download className="h-3 w-3" />
                  Скачать все ({t.result_files.length})
                </a>
              )}
            </div>
            <div className="space-y-1">
              {t.result_files.map((f: string) => {
                const fileName = f.split('/').pop() || f
                return (
                  <a key={f} href={`/api/tasks/${t.id}/files/${fileName}`} download
                    className="flex items-center gap-2 text-sm text-blue-600 hover:underline">
                    <Download className="h-4 w-4" />
                    {fileName}
                  </a>
                )
              })}
            </div>
          </div>
        )}

        {/* Token usage */}
        {t.token_usage && (t.token_usage.input_tokens || t.token_usage.output_tokens) && (
          <div className="text-xs text-gray-400">
            Tokens: {(t.token_usage.input_tokens || 0).toLocaleString()} in / {(t.token_usage.output_tokens || 0).toLocaleString()} out
          </div>
        )}

        {/* Quality profile (E-02) */}
        {isTerminal && (
          <div className="pt-2 border-t">
            {profile ? (
              <QualityRadarChart profile={profile} />
            ) : (
              <div className="flex items-center justify-between">
                <span className="text-sm text-gray-400">Not yet evaluated for quality.</span>
              </div>
            )}
            <button
              onClick={() => evaluateMutation.mutate()}
              disabled={evaluateMutation.isPending}
              className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              <Gauge className="h-4 w-4" />
              {evaluateMutation.isPending ? 'Evaluating…' : profile ? 'Re-evaluate' : 'Evaluate quality'}
            </button>
            {evaluateMutation.data?.skipped && (
              <p className="text-xs text-orange-600 mt-1">
                Skipped: {evaluateMutation.data.detail}
              </p>
            )}

            {/* Human feedback (E-05) */}
            <HumanFeedbackForm key={humanFeedback?.submitted_at ?? 'new'} taskId={task.id} profile={profile} existing={humanFeedback} />
            {humanFeedback && (
              <p className="text-xs text-gray-400 mt-1">
                Last feedback by {humanFeedback.submitted_by}
                {humanFeedback.verdict ? ` · ${humanFeedback.verdict}` : ''}
              </p>
            )}

            {/* Cleaned trace preview (E-06) — input for the trajectory judge */}
            <CleanedTracePanel taskId={task.id} />

            {/* Trajectory score (E-07) — 6-axis judge of how the agent worked */}
            <TrajectoryScorePanel taskId={task.id} />

            {/* Evidence bank score (E-08) — TRACE per-step judge with groundedness */}
            <EvidenceBankPanel taskId={task.id} />

            {/* Trajectory match (E-09) — deterministic match vs a canonical trajectory */}
            <TrajectoryMatchPanel taskId={task.id} />

            {/* Capability isolation (E-13) — did the agent really use the required tool? */}
            <CapabilityPanel taskId={task.id} />

            {/* Variance / robustness (E-11) — replay this task N times, measure dispersion */}
            <VarianceRunPanel taskId={task.id} />

            {/* Adversarial / perturbation (E-12) — perturb the input, measure robustness */}
            <PerturbationPanel taskId={task.id} />

            {/* Failure modes (E-14) — classify the type(s) of failure in the trajectory */}
            <FailureModePanel taskId={task.id} />

            {/* Hallucinations (E-15) — fact-check the deliverable's URLs/APIs/numbers/citations */}
            <HallucinationPanel taskId={task.id} />

            {/* Calibration (E-16) — self-probe confidence vs actual correctness */}
            <CalibrationPanel taskId={task.id} />
          </div>
        )}

        {/* Subtasks */}
        {detail?.subtasks && detail.subtasks.length > 0 && (
          <div>
            <h3 className="text-sm font-medium text-gray-500 mb-1">Subtasks</h3>
            <div className="space-y-1">
              {detail.subtasks.map(sub => (
                <div key={sub.id} className="flex items-center gap-2 text-sm p-2 bg-gray-50 rounded">
                  <span className="text-xs px-1.5 py-0.5 rounded bg-gray-200">{sub.status}</span>
                  <span className="text-gray-700">{sub.title}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Start button for backlog tasks */}
        {t.status === 'backlog' && (
          <div className="pt-2 border-t">
            <button
              onClick={() => startMutation.mutate()}
              disabled={startMutation.isPending}
              className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              <Play className="h-4 w-4" /> Start Task
            </button>
          </div>
        )}

        {/* Approve / Reject buttons */}
        {t.status === 'awaiting_approval' && (
          <div className="space-y-2 pt-2 border-t">
            <div className="flex gap-2">
              <button
                onClick={() => approveMutation.mutate()}
                disabled={approveMutation.isPending}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
              >
                <Check className="h-4 w-4" /> Approve
              </button>
              <button
                onClick={() => setShowReject(!showReject)}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
              >
                <RotateCcw className="h-4 w-4" /> Reject
              </button>
            </div>
            {showReject && (
              <div className="space-y-2">
                <textarea
                  value={rejectFeedback}
                  onChange={e => setRejectFeedback(e.target.value)}
                  placeholder="Feedback for the agent..."
                  className="w-full p-2 border rounded-lg text-sm resize-none h-20"
                />
                <button
                  onClick={() => rejectMutation.mutate()}
                  disabled={rejectMutation.isPending}
                  className="w-full px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm disabled:opacity-50"
                >
                  Send rejection
                </button>
              </div>
            )}
          </div>
        )}

        <ReasoningTimeline events={events} />

        {['in_progress', 'review', 'awaiting_approval', 'done', 'failed'].includes(t.status) && (
          <AgentLogViewer taskId={t.id} archived={!!t.log_archive_s3_path} />
        )}

        {/* Event log */}
        {events.length > 0 && (
          <div>
            <h3 className="text-sm font-medium text-gray-500 mb-2">Events</h3>
            <div className="space-y-1.5 max-h-60 overflow-y-auto">
              {events.map(ev => (
                <div key={ev.id} className="flex items-start gap-2 text-xs">
                  <span className={cn('px-1.5 py-0.5 rounded', SOURCE_COLORS[ev.source] || 'bg-gray-100')}>
                    {ev.source}
                  </span>
                  <span className="text-gray-600 flex-1">{ev.event_type}</span>
                  <span className="text-gray-400 flex items-center gap-1">
                    <Clock className="h-3 w-3" />
                    {formatDistanceToNow(new Date(ev.created_at))}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
