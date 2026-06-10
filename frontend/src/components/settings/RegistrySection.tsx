import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { registryApi } from '@/api/client'
import type { RegistryEntry, RegistryKind } from '@/types'
import { Wrench, Plus, Trash, X, Plug, CheckCircle2, XCircle } from 'lucide-react'

interface DraftState {
  name: string
  kind: RegistryKind
  command: string
  args: string // space-separated
  url: string
  secrets: string // KEY=VAL per line
}

const EMPTY: DraftState = { name: '', kind: 'builtin', command: '', args: '', url: '', secrets: '' }

function parseSecrets(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t) continue
    const eq = t.indexOf('=')
    if (eq > 0) out[t.slice(0, eq).trim()] = t.slice(eq + 1).trim()
  }
  return out
}

export function RegistrySection({ canEdit }: { canEdit: boolean }) {
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<DraftState>(EMPTY)
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; detail: string }>>({})

  const { data: entries = [] } = useQuery({
    queryKey: ['registry', 'tools'],
    queryFn: () => registryApi.list(),
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['registry', 'tools'] })

  const create = useMutation({
    mutationFn: () => {
      const config: Record<string, unknown> =
        draft.kind === 'mcp'
          ? {
              ...(draft.command.trim() ? { command: draft.command.trim() } : {}),
              ...(draft.args.trim() ? { args: draft.args.trim().split(/\s+/) } : {}),
              ...(draft.url.trim() ? { url: draft.url.trim() } : {}),
            }
          : {}
      return registryApi.create({
        name: draft.name.trim(),
        kind: draft.kind,
        config,
        secrets: parseSecrets(draft.secrets),
      })
    },
    onSuccess: () => {
      invalidate()
      setDraft(EMPTY)
      setAdding(false)
    },
  })

  const toggleEnabled = useMutation({
    mutationFn: (e: RegistryEntry) => registryApi.update(e.id, { enabled: !e.enabled }),
    onSuccess: invalidate,
  })

  const remove = useMutation({
    mutationFn: async (id: string) => {
      try {
        await registryApi.remove(id)
      } catch (err) {
        // 409 → referenced by templates; offer to force.
        if (confirm('This entry is referenced by templates. Remove it and strip those references?')) {
          await registryApi.remove(id, true)
        } else {
          throw err
        }
      }
    },
    onSuccess: invalidate,
  })

  const test = useMutation({
    mutationFn: (id: string) => registryApi.test(id),
    onSuccess: (r, id) => setTestResult((prev) => ({ ...prev, [id]: r })),
  })

  const canSubmit =
    draft.name.trim() && (draft.kind === 'builtin' || draft.command.trim() || draft.url.trim())

  return (
    <div className="bg-white rounded-lg border p-4 mb-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="font-semibold flex items-center gap-2">
            <Wrench className="h-4 w-4" /> Tool &amp; MCP Registry
            <span className="text-xs font-normal text-gray-400">(SPA-41)</span>
          </h2>
          <p className="text-xs text-gray-500">
            Configure tools and MCP servers once; templates reference them by id. Secrets are masked
            here and only revealed to the agent at spawn.
          </p>
        </div>
        {canEdit && !adding && (
          <button
            onClick={() => setAdding(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
          >
            <Plus className="h-4 w-4" /> Add
          </button>
        )}
      </div>

      {adding && (
        <div className="border rounded-lg p-3 mb-3 bg-gray-50 space-y-2.5">
          <div className="flex items-center gap-2">
            <input
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              placeholder="name (e.g. web_search, github)"
              className="flex-1 px-3 py-1.5 border rounded-lg text-sm"
            />
            <select
              value={draft.kind}
              onChange={(e) => setDraft({ ...draft, kind: e.target.value as RegistryKind })}
              className="px-3 py-1.5 border rounded-lg text-sm"
            >
              <option value="builtin">builtin</option>
              <option value="mcp">mcp</option>
            </select>
          </div>
          {draft.kind === 'mcp' && (
            <div className="space-y-2">
              <div className="grid grid-cols-2 gap-2">
                <input
                  value={draft.command}
                  onChange={(e) => setDraft({ ...draft, command: e.target.value })}
                  placeholder="command (e.g. npx) — stdio"
                  className="px-3 py-1.5 border rounded-lg text-sm"
                />
                <input
                  value={draft.url}
                  onChange={(e) => setDraft({ ...draft, url: e.target.value })}
                  placeholder="url (http MCP) — optional"
                  className="px-3 py-1.5 border rounded-lg text-sm"
                />
              </div>
              <input
                value={draft.args}
                onChange={(e) => setDraft({ ...draft, args: e.target.value })}
                placeholder="args (space-separated, e.g. -y @scope/server)"
                className="w-full px-3 py-1.5 border rounded-lg text-sm"
              />
              <textarea
                value={draft.secrets}
                onChange={(e) => setDraft({ ...draft, secrets: e.target.value })}
                placeholder="secrets (KEY=VALUE per line, e.g. GITHUB_TOKEN=ghp_…)"
                className="w-full px-3 py-1.5 border rounded-lg text-sm font-mono h-16 resize-none"
              />
            </div>
          )}
          <div className="flex items-center gap-2">
            <button
              onClick={() => create.mutate()}
              disabled={!canSubmit || create.isPending}
              className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
            >
              Add entry
            </button>
            <button
              onClick={() => {
                setAdding(false)
                setDraft(EMPTY)
              }}
              className="p-1.5 rounded hover:bg-gray-100"
            >
              <X className="h-4 w-4" />
            </button>
            {create.isError && <span className="text-xs text-red-600">Create failed — name must be unique.</span>}
          </div>
        </div>
      )}

      {entries.length === 0 ? (
        <p className="text-sm text-gray-400">No registry entries yet.</p>
      ) : (
        <div className="border rounded-lg divide-y">
          {entries.map((e) => (
            <div key={e.id} className="flex items-center gap-2 px-3 py-2 text-sm">
              <span className="font-medium text-gray-800">{e.name}</span>
              <span
                className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border ${
                  e.kind === 'mcp'
                    ? 'bg-purple-50 text-purple-700 border-purple-200'
                    : 'bg-gray-50 text-gray-500 border-gray-200'
                }`}
              >
                {e.kind}
              </span>
              {e.secret_keys.length > 0 && (
                <span className="text-[10px] text-gray-400">{e.secret_keys.length} secret(s)</span>
              )}
              {testResult[e.id] && (
                <span title={testResult[e.id].detail} className="flex items-center">
                  {testResult[e.id].ok ? (
                    <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />
                  ) : (
                    <XCircle className="h-3.5 w-3.5 text-red-500" />
                  )}
                </span>
              )}
              <div className="ml-auto flex items-center gap-1">
                <button
                  onClick={() => test.mutate(e.id)}
                  disabled={test.isPending}
                  title="Test connection"
                  className="p-1 rounded hover:bg-gray-100"
                >
                  <Plug className="h-4 w-4 text-gray-400" />
                </button>
                {canEdit && (
                  <>
                    <button
                      onClick={() => toggleEnabled.mutate(e)}
                      className={`text-[11px] px-1.5 py-0.5 rounded border ${
                        e.enabled
                          ? 'bg-green-50 text-green-700 border-green-200'
                          : 'bg-gray-100 text-gray-500 border-gray-200'
                      }`}
                    >
                      {e.enabled ? 'enabled' : 'disabled'}
                    </button>
                    <button
                      onClick={() => remove.mutate(e.id)}
                      className="p-1 rounded hover:bg-red-50"
                      title="Delete"
                    >
                      <Trash className="h-4 w-4 text-gray-400 hover:text-red-500" />
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      {Object.entries(testResult).some(([id]) => entries.find((e) => e.id === id)) && (
        <p className="text-xs text-gray-400 mt-2">
          Tip: stdio MCP servers validate shape here; the live handshake runs in the agent sandbox.
        </p>
      )}
    </div>
  )
}
