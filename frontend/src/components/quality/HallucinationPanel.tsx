import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ScanSearch, RefreshCw, AlertCircle, CheckCircle2, XCircle } from 'lucide-react'
import { qualityApi } from '@/api/client'
import type { HallucinationProfile, HallucinationCategory, HallucinationItem } from '@/types'
import { cn } from '@/lib/utils'

/** Hallucination Detection (E-15): a fact-check of the agent's deliverable across
 *  four categories — URLs, APIs, numbers, citations. URLs/known APIs are checked
 *  deterministically against the trace; numbers, claims and uncertain APIs go to
 *  a single LLM call. A clean deliverable yields rate 0. */

interface Props {
  taskId: string
}

const CATEGORY_LABEL: Record<HallucinationCategory, string> = {
  urls: 'URLs',
  apis: 'APIs',
  numbers: 'Numbers',
  citations: 'Citations',
}
const CATEGORY_ORDER: HallucinationCategory[] = ['urls', 'apis', 'numbers', 'citations']

export default function HallucinationPanel({ taskId }: Props) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data, isFetching } = useQuery({
    queryKey: ['hallucination-profile', taskId],
    queryFn: () => qualityApi.getHallucinations(taskId),
    enabled: open,
    retry: false,
  })
  const profile = data?.hallucination_profile ?? null

  const evaluate = useMutation({
    mutationFn: () => qualityApi.evaluateHallucinations(taskId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['hallucination-profile', taskId] }),
  })

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="mt-2 flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
      >
        <ScanSearch className="h-4 w-4" />
        Hallucinations
      </button>
    )
  }

  return (
    <div className="mt-2 border rounded-lg p-3 bg-gray-50 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Hallucinations (E-15)</h4>
        <button onClick={() => setOpen(false)} className="text-xs text-gray-400 hover:underline">
          close
        </button>
      </div>

      {isFetching && <p className="text-xs text-gray-400">Loading…</p>}
      {!isFetching && profile && <ProfileView profile={profile} />}
      {!isFetching && !profile && <p className="text-xs text-gray-400">Not yet evaluated.</p>}

      {evaluate.isError && <p className="text-xs text-red-600">Evaluate request failed.</p>}
      {evaluate.data?.skipped && (
        <p className="text-xs text-amber-600">{evaluate.data.detail}</p>
      )}

      <button
        onClick={() => evaluate.mutate()}
        disabled={evaluate.isPending}
        className="flex items-center gap-2 px-3 py-1.5 border rounded-lg text-sm hover:bg-white disabled:opacity-50"
      >
        <RefreshCw className={cn('h-4 w-4', evaluate.isPending && 'animate-spin')} />
        {evaluate.isPending ? 'Fact-checking…' : profile ? 'Re-evaluate' : 'Fact-check'}
      </button>
    </div>
  )
}

function ProfileView({ profile }: { profile: HallucinationProfile }) {
  if (profile.status === 'error') {
    return (
      <div className="flex items-start gap-2 text-xs text-red-600">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span>Fact-check error: {profile.errors[0]?.error ?? 'unknown'}</span>
      </div>
    )
  }

  const clean = profile.hallucination_count === 0

  return (
    <>
      <div className="flex items-center gap-2 text-xs">
        {clean ? (
          <span className="flex items-center gap-1 text-green-700">
            <CheckCircle2 className="h-4 w-4 shrink-0" /> No hallucinations detected.
          </span>
        ) : (
          <span className="flex items-center gap-1 text-red-700">
            <XCircle className="h-4 w-4 shrink-0" />
            {profile.hallucination_count} hallucinated / {profile.items_total} checked
          </span>
        )}
        <span className="ml-auto px-2 py-0.5 rounded-full font-medium bg-gray-200 text-gray-700 tabular-nums">
          rate {Math.round(profile.hallucination_rate * 100)}%
        </span>
      </div>

      <div className="space-y-2">
        {CATEGORY_ORDER.map((cat) => (
          <CategoryView key={cat} label={CATEGORY_LABEL[cat]} block={profile.categories[cat]} />
        ))}
      </div>

      {profile.summary && <p className="text-xs text-gray-600 border-t pt-2">{profile.summary}</p>}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 border-t pt-2">
        <span className="text-gray-700">{profile.judge_model}</span>
        <span>${profile.judge_cost_usd.toFixed(4)}</span>
        <span>
          {profile.judge_input_tokens}/{profile.judge_output_tokens} tok
        </span>
        {profile.used_trajectory_evidence && <span>+E-08</span>}
        {profile.used_outcome_profile && <span>+E-02</span>}
        {profile.input_capped && <span className="text-amber-600">input capped</span>}
      </div>
    </>
  )
}

function CategoryView({
  label,
  block,
}: {
  label: string
  block: HallucinationProfile['categories'][HallucinationCategory]
}) {
  if (!block || block.checked === 0) {
    return (
      <div className="text-xs text-gray-400">
        <span className="font-medium text-gray-500">{label}</span> — nothing to check
      </div>
    )
  }
  return (
    <div className="text-xs">
      <div className="flex items-center gap-2">
        <span className="font-medium text-gray-700">{label}</span>
        <span
          className={cn(
            'tabular-nums',
            block.hallucinated > 0 ? 'text-red-600' : 'text-green-700',
          )}
        >
          {block.hallucinated} / {block.checked} hallucinated
        </span>
      </div>
      {block.items.length > 0 && (
        <ul className="mt-1 ml-1 space-y-1">
          {block.items.map((it, i) => (
            <ItemRow key={i} item={it} />
          ))}
        </ul>
      )}
    </div>
  )
}

function ItemRow({ item }: { item: HallucinationItem }) {
  const text = item.value ?? item.claim ?? ''
  return (
    <li className="text-gray-600">
      <div className="flex items-start gap-1.5">
        <XCircle className="h-3.5 w-3.5 shrink-0 text-red-500 mt-0.5" />
        <span className="flex-1">
          <span className="break-all font-mono text-[11px] text-gray-800">{text}</span>
          {typeof item.confidence === 'number' && (
            <span className="ml-1 text-gray-400 tabular-nums">
              ({Math.round(item.confidence * 100)}%)
            </span>
          )}
          {item.reason && <span className="block text-gray-500">{item.reason}</span>}
        </span>
      </div>
    </li>
  )
}
