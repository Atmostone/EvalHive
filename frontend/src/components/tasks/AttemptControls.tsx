import { useCallback, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { attemptsApi } from '@/api/client'
import { useAuth } from '@/stores/auth'
import type { AttemptEvent, ExecutionAttempt, JsonSchema } from '@/types'
import { configurationErrors } from '@/lib/harnessSchema'
import { mergeInteractions } from '@/lib/harnessInteractions'
import { HarnessSettingsEditor } from '../settings/HarnessSettingsEditor'

function CommandForm({ schema, initial, submit, disabled, label }: {
  schema: JsonSchema; initial: Record<string, unknown>; submit: (payload: Record<string, unknown>) => void
  disabled: boolean; label: string
}) {
  const [payload, setPayload] = useState(initial)
  const [valid, setValid] = useState(true)
  const validity = useCallback((value: boolean) => setValid(value), [])
  const errors = configurationErrors(schema, payload)
  const editable = Object.fromEntries(Object.entries(payload).filter(([key]) => key !== 'request_id'))
  const editableSchema = { ...schema,
    properties: Object.fromEntries(Object.entries(schema.properties ?? {}).filter(([key]) => key !== 'request_id')),
    required: schema.required?.filter(key => key !== 'request_id'),
  }
  return <fieldset disabled={disabled} className="space-y-2">
    <HarnessSettingsEditor schema={editableSchema} value={editable} onChange={next => setPayload({ ...next, ...initial })} onValidity={validity} />
    <button type="button" disabled={disabled || !valid || errors.length > 0}
      className="text-sm text-blue-600 disabled:opacity-50" onClick={() => submit(payload)}>{label}</button>
  </fieldset>
}

function LiveControls({ attempt }: { attempt: ExecutionAttempt }) {
  const queryClient = useQueryClient()
  const role = useAuth(state => state.workspaces.find(row => row.id === state.workspaceId)?.role)
  const canControl = role === 'owner' || role === 'admin'
  const canFeedback = canControl || role === 'member'
  const active = ['created', 'running', 'stopping'].includes(attempt.status)
  const [cursor, setCursor] = useState(-1)
  const [requests, setRequests] = useState<Record<string, AttemptEvent>>({})
  const [feedback, setFeedback] = useState('')
  const [submission, setSubmission] = useState<Record<string, string>>({})
  const events = useQuery({ queryKey: ['attempt-interactions', attempt.id, cursor],
    queryFn: () => attemptsApi.events(attempt.id, cursor), gcTime: 0,
    refetchInterval: active ? 1000 : false,
  })
  const commands = useQuery({ queryKey: ['attempt-commands', attempt.id],
    queryFn: () => attemptsApi.commands(attempt.id), refetchInterval: active ? 1000 : false,
  })
  useEffect(() => {
    if (!events.data?.length) return
    setRequests(previous => mergeInteractions(previous, events.data!))
    setCursor(events.data[events.data.length - 1].seq)
  }, [events.data])
  const control = useMutation({
    mutationFn: ({ name, payload, id }: { name: string; payload: Record<string, unknown>; id: string }) =>
      attemptsApi.control(attempt.id, name, payload, id),
    onSuccess: (_, variables) => {
      if (variables.name === 'feedback') setFeedback('')
      setSubmission(previous => {
        const next = { ...previous }; delete next[JSON.stringify([variables.name, variables.payload])]; return next
      })
      queryClient.invalidateQueries({ queryKey: ['attempt-commands', attempt.id] })
      queryClient.invalidateQueries({ queryKey: ['task-attempts'] })
    },
  })
  const send = (name: string, payload: Record<string, unknown>) => {
    // A failed HTTP response can follow a committed command. Reuse its ID when
    // the user retries exactly the same action instead of duplicating feedback.
    const key = JSON.stringify([name, payload])
    const id = submission[key] ?? crypto.randomUUID()
    setSubmission(previous => ({ ...previous, [key]: id }))
    control.mutate({ name, payload, id })
  }
  const error = events.error ?? commands.error ?? control.error
  return <div className="border rounded-lg p-3 space-y-3">
    <div className="text-sm font-medium">Attempt {attempt.number} · {attempt.connector_id} · {attempt.status}</div>
    {error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    {active && <>
      {canFeedback && ['native', 'emulated'].includes(attempt.capabilities.feedback) ? <div className="space-y-2">
        <textarea aria-label="Feedback for the running attempt" placeholder="Feedback for this attempt" value={feedback}
          onChange={event => setFeedback(event.target.value)} className="w-full border rounded p-2 text-sm" />
        <button type="button" disabled={!feedback.trim() || control.isPending || attempt.status === 'stopping'}
          className="text-sm text-blue-600 disabled:opacity-50" onClick={() => send('feedback', { text: feedback })}>Send feedback</button>
      </div> : <p className="text-xs text-gray-500">Live feedback is unavailable.</p>}
      {canControl && <button type="button" disabled={control.isPending || attempt.status === 'stopping'}
        className="text-sm text-red-600 disabled:opacity-50" onClick={() => send('abort', {})}>Cancel execution</button>}
      {Object.values(requests).map(request => {
        const kind = request.data.kind ?? ''
        const schema = attempt.commands[kind]?.input_schema
        const pending = commands.data?.find(command => command.request_id === request.data.request_id
          && ['queued', 'acknowledged', 'unknown'].includes(command.status))
        return <div key={request.data.request_id} className="border rounded p-2 space-y-2">
          <p className="text-sm font-medium">Action requested: {kind}</p>
          <pre className="whitespace-pre-wrap text-xs max-h-40 overflow-auto">{JSON.stringify(request.data.payload, null, 2)?.slice(0, 8000)}</pre>
          {schema && ['native', 'emulated'].includes(attempt.capabilities[kind]) ? <CommandForm
            schema={schema} initial={schema.properties?.request_id ? { request_id: request.data.request_id } : {}}
            disabled={!canControl || control.isPending || !!pending || attempt.status === 'stopping'} label="Submit action"
            submit={payload => send(kind, payload)} /> : <p className="text-sm text-red-600">This interaction is unsupported.</p>}
        </div>
      })}
    </>}
    {!!commands.data?.length && <details><summary className="text-xs cursor-pointer">Control receipts</summary>
      {commands.data.map(command => <p key={command.id} className="text-xs mt-1">
        {command.name}: {command.status}{command.error ? ` · ${command.error}` : ''}
      </p>)}
    </details>}
    {!active && attempt.result && <p className="text-sm">{attempt.result.summary}</p>}
  </div>
}

export default function AttemptControls({ taskId }: { taskId: string }) {
  const attempts = useQuery({ queryKey: ['task-attempts', taskId], queryFn: () => attemptsApi.list(taskId), refetchInterval: 1000 })
  if (attempts.error) return <p role="alert" className="text-sm text-red-600">{attempts.error.message}</p>
  return attempts.data?.[0] ? <LiveControls key={attempts.data[0].id} attempt={attempts.data[0]} /> : null
}
