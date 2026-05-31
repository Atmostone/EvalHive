import { authHeaders, useAuth } from '../stores/auth'

const BASE = '/api'

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const isFormData = options?.body instanceof FormData
  const headers: Record<string, string> = { ...(authHeaders() as Record<string, string>) }
  if (!isFormData) headers['Content-Type'] = 'application/json'
  if (options?.headers) {
    for (const [k, v] of Object.entries(options.headers as Record<string, string>)) {
      headers[k] = v
    }
  }
  const res = await fetch(`${BASE}${path}`, { ...options, headers })
  if (!res.ok) {
    if (res.status === 401) {
      useAuth.getState().logout()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.assign('/login')
      }
    }
    const text = await res.text()
    throw new ApiError(res.status, text)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

// Auth
export const authApi = {
  login: (data: { email: string; password: string }) =>
    request<{
      access_token: string
      token_type: string
      expires_in: number
      user: { id: string; email: string; display_name?: string | null }
      default_workspace_id: string
    }>('/auth/login', { method: 'POST', body: JSON.stringify(data) }),
  register: (data: { email: string; password: string; display_name?: string }) =>
    request<{
      access_token: string
      token_type: string
      expires_in: number
      user: { id: string; email: string; display_name?: string | null }
      default_workspace_id: string
    }>('/auth/register', { method: 'POST', body: JSON.stringify(data) }),
  me: () =>
    request<{
      user: { id: string; email: string; display_name?: string | null }
      workspaces: { id: string; name: string; slug: string; role: string }[]
    }>('/auth/me'),
}

// Tasks
export const tasksApi = {
  list: (params?: { status?: string; parent_id?: string }) => {
    const qs = new URLSearchParams(params as Record<string, string>).toString()
    return request<Task[]>(`/tasks${qs ? `?${qs}` : ''}`)
  },
  get: (id: string) => request<Task & { subtasks: Task[] }>(`/tasks/${id}`),
  create: (data: { title: string; description?: string; priority?: string; reference_answer?: string }) =>
    request<Task>('/tasks', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<Task>) =>
    request<Task>(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  approve: (id: string) =>
    request<Task>(`/tasks/${id}/approve`, { method: 'PATCH' }),
  reject: (id: string, feedback: string) =>
    request<Task>(`/tasks/${id}/reject`, { method: 'PATCH', body: JSON.stringify({ feedback }) }),
  delete: (id: string) =>
    request<{ status: string }>(`/tasks/${id}`, { method: 'DELETE' }),
  getDecomposition: (id: string) =>
    request<import('../types').DecompositionResponse>(`/tasks/${id}/decomposition`),
}

// Templates
export const templatesApi = {
  list: () => request<Template[]>('/templates'),
  get: (id: string) => request<Template>(`/templates/${id}`),
  create: (data: Omit<Template, 'id' | 'created_at' | 'updated_at'>) =>
    request<Template>('/templates', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<Template>) =>
    request<Template>(`/templates/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) =>
    request<{ status: string }>(`/templates/${id}`, { method: 'DELETE' }),
}

// Agents
export interface SwitchModelBody {
  model_id: string
}

export const agentsApi = {
  list: () => request<Agent[]>('/agents'),
  get: (id: string) => request<Agent>(`/agents/${id}`),
  kill: (id: string) =>
    request<{ status: string }>(`/agents/${id}/kill`, { method: 'POST' }),
  killAll: () =>
    request<{ status: string; killed: number }>('/agents/kill-all', { method: 'POST' }),
  switchModel: (containerId: string, body: SwitchModelBody) =>
    request<{ status: string }>(`/agents/${containerId}/switch_model`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}

// Events
export const eventsApi = {
  list: (params?: {
    task_id?: string
    agent_container_id?: string
    event_type?: string
    source?: string
    limit?: number
    from_dt?: string
    to_dt?: string
  }) => {
    const qs = new URLSearchParams()
    if (params) {
      Object.entries(params).forEach(([k, v]) => { if (v != null) qs.set(k, String(v)) })
    }
    const qstr = qs.toString()
    return request<AgentEvent[]>(`/events${qstr ? `?${qstr}` : ''}`)
  },
}

// Agent logs
import type { LogChunksResponse } from '../types'

export const logsApi = {
  list: (taskId: string, params?: { from_seq?: number; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params?.from_seq != null) qs.set('from_seq', String(params.from_seq))
    if (params?.limit != null) qs.set('limit', String(params.limit))
    const qstr = qs.toString()
    return request<LogChunksResponse>(`/tasks/${taskId}/log${qstr ? `?${qstr}` : ''}`)
  },
}

// Settings
export const settingsApi = {
  get: () => request<Record<string, unknown>>('/settings'),
  update: (data: Record<string, unknown>) =>
    request<{ status: string }>('/settings', { method: 'PATCH', body: JSON.stringify(data) }),
}

// Providers & Models
import type { Provider, LLMModel, ModelTestResponse, SystemModels } from '../types'

export const providersApi = {
  list: () => request<Provider[]>('/providers'),
  create: (data: { name: string; api_key: string; endpoint: string }) =>
    request<Provider>('/providers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: { name?: string; api_key?: string; endpoint?: string }) =>
    request<Provider>(`/providers/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  remove: (id: string) =>
    request<void>(`/providers/${id}`, { method: 'DELETE' }),
  listModels: (providerId: string) =>
    request<LLMModel[]>(`/providers/${providerId}/models`),
  createModel: (
    providerId: string,
    data: {
      display_name: string
      api_name: string
      input_price_per_1m_usd?: number
      output_price_per_1m_usd?: number
    },
  ) =>
    request<LLMModel>(`/providers/${providerId}/models`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

export const modelsApi = {
  update: (
    id: string,
    data: {
      display_name?: string
      api_name?: string
      input_price_per_1m_usd?: number
      output_price_per_1m_usd?: number
    },
  ) =>
    request<LLMModel>(`/models/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  remove: (id: string) =>
    request<void>(`/models/${id}`, { method: 'DELETE' }),
  test: (id: string) =>
    request<ModelTestResponse>(`/models/${id}/test`, { method: 'POST' }),
}

export const workspaceApi = {
  getSystemModels: () => request<SystemModels>('/workspaces/me/system-models'),
  updateSystemModels: (data: Partial<SystemModels>) =>
    request<SystemModels>('/workspaces/me/system-models', {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
}

// Quality Rubric Engine (E-02)
import type { Rubric, QualityProfile, HumanFeedback, CleanedTrace, TrajectoryProfile, TrajectoryEvidenceProfile, TrajectoryMatchProfile, CapabilityProfile, CapabilityAggregate, FailureProfile, HallucinationProfile, CalibrationProfile, CalibrationAggregate, JudgeCalibration, JudgeCalibrationBadge } from '../types'

type RubricInput = Pick<Rubric, 'name' | 'description' | 'applies_to' | 'is_default' | 'dimensions'>

export const rubricsApi = {
  list: () => request<Rubric[]>('/quality/rubrics'),
  get: (id: string) => request<Rubric>(`/quality/rubrics/${id}`),
  create: (data: RubricInput) =>
    request<Rubric>('/quality/rubrics', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: string, data: Partial<RubricInput>) =>
    request<Rubric>(`/quality/rubrics/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  remove: (id: string) =>
    request<{ ok: boolean }>(`/quality/rubrics/${id}`, { method: 'DELETE' }),
}

export const qualityApi = {
  getProfile: (taskId: string) =>
    request<{ task_id: string; quality_profile: QualityProfile | null }>(
      `/quality/records/${taskId}/profile`,
    ),
  evaluate: (taskId: string) =>
    request<{ task_id: string; quality_profile: QualityProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate`,
      { method: 'POST' },
    ),
  getFeedback: (taskId: string) =>
    request<{ task_id: string; human_feedback: HumanFeedback | null }>(
      `/quality/records/${taskId}/feedback`,
    ),
  saveFeedback: (taskId: string, body: HumanFeedbackInput) =>
    request<{ task_id: string; human_feedback: HumanFeedback }>(
      `/quality/records/${taskId}/feedback`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),
  getCleanedTrace: (
    taskId: string,
    params?: { tool_output_token_cap?: number; keep_tail_on_error?: boolean },
  ) => {
    const q = new URLSearchParams()
    if (params?.tool_output_token_cap != null) q.set('tool_output_token_cap', String(params.tool_output_token_cap))
    if (params?.keep_tail_on_error) q.set('keep_tail_on_error', 'true')
    const qs = q.toString()
    return request<{ task_id: string; cleaned_trace: CleanedTrace }>(
      `/quality/records/${taskId}/trace${qs ? `?${qs}` : ''}`,
    )
  },
  getTrajectoryProfile: (taskId: string) =>
    request<{ task_id: string; trajectory_profile: TrajectoryProfile | null }>(
      `/quality/records/${taskId}/trajectory`,
    ),
  evaluateTrajectory: (taskId: string) =>
    request<{ task_id: string; trajectory_profile: TrajectoryProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate-trajectory`,
      { method: 'POST' },
    ),
  getTraceEvidenceProfile: (taskId: string) =>
    request<{ task_id: string; trajectory_evidence_profile: TrajectoryEvidenceProfile | null }>(
      `/quality/records/${taskId}/trajectory-evidence`,
    ),
  evaluateTraceEvidence: (taskId: string) =>
    request<{ task_id: string; trajectory_evidence_profile: TrajectoryEvidenceProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate-trajectory-evidence`,
      { method: 'POST' },
    ),
  getTrajectoryMatch: (taskId: string) =>
    request<{ task_id: string; trajectory_match_profile: TrajectoryMatchProfile | null }>(
      `/quality/records/${taskId}/trajectory-match`,
    ),
  evaluateTrajectoryMatch: (taskId: string) =>
    request<{ task_id: string; trajectory_match_profile: TrajectoryMatchProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate-trajectory-match`,
      { method: 'POST' },
    ),
  getCapability: (taskId: string) =>
    request<{ task_id: string; capability_profile: CapabilityProfile | null }>(
      `/quality/records/${taskId}/capability`,
    ),
  evaluateCapability: (taskId: string) =>
    request<{ task_id: string; capability_profile: CapabilityProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate-capability`,
      { method: 'POST' },
    ),
  getCapabilityAggregate: (params?: { category?: string; model_used?: string; template_id?: string }) => {
    const q = new URLSearchParams()
    if (params?.category) q.set('category', params.category)
    if (params?.model_used) q.set('model_used', params.model_used)
    if (params?.template_id) q.set('template_id', params.template_id)
    const qs = q.toString()
    return request<CapabilityAggregate>(`/quality/capability/aggregate${qs ? `?${qs}` : ''}`)
  },
  getFailureModes: (taskId: string) =>
    request<{ task_id: string; failure_profile: FailureProfile | null }>(
      `/quality/records/${taskId}/failure-modes`,
    ),
  evaluateFailureModes: (taskId: string) =>
    request<{ task_id: string; failure_profile: FailureProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate-failure-modes`,
      { method: 'POST' },
    ),
  getHallucinations: (taskId: string) =>
    request<{ task_id: string; hallucination_profile: HallucinationProfile | null }>(
      `/quality/records/${taskId}/hallucinations`,
    ),
  evaluateHallucinations: (taskId: string) =>
    request<{ task_id: string; hallucination_profile: HallucinationProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate-hallucinations`,
      { method: 'POST' },
    ),
  getCalibration: (taskId: string) =>
    request<{ task_id: string; calibration_profile: CalibrationProfile | null }>(
      `/quality/records/${taskId}/calibration`,
    ),
  evaluateCalibration: (taskId: string) =>
    request<{ task_id: string; calibration_profile: CalibrationProfile | null; skipped: boolean; detail?: string }>(
      `/quality/records/${taskId}/evaluate-calibration`,
      { method: 'POST' },
    ),
  getCalibrationAggregate: (params?: { model_used?: string; template_id?: string; suite?: string; bins?: number }) => {
    const q = new URLSearchParams()
    if (params?.model_used) q.set('model_used', params.model_used)
    if (params?.template_id) q.set('template_id', params.template_id)
    if (params?.suite) q.set('suite', params.suite)
    if (params?.bins) q.set('bins', String(params.bins))
    const qs = q.toString()
    return request<CalibrationAggregate>(`/quality/calibration/aggregate${qs ? `?${qs}` : ''}`)
  },
  runJudgeCalibration: (body?: { suite?: string; template_id?: string }) =>
    request<JudgeCalibration>('/quality/judge-calibration/run', {
      method: 'POST',
      body: JSON.stringify(body ?? {}),
    }),
  getJudgeCalibration: (params?: { judge_config_key?: string; history?: boolean }) => {
    const q = new URLSearchParams()
    if (params?.judge_config_key) q.set('judge_config_key', params.judge_config_key)
    if (params?.history) q.set('history', 'true')
    const qs = q.toString()
    return request<
      JudgeCalibration | null | { latest: JudgeCalibration | null; history: JudgeCalibration[] }
    >(`/quality/judge-calibration${qs ? `?${qs}` : ''}`)
  },
  getJudgeCalibrationBadge: () =>
    request<JudgeCalibrationBadge>('/quality/judge-calibration/badge'),
}

export interface HumanFeedbackInput {
  verdict?: 'approve' | 'reject' | null
  overall_comment?: string | null
  dimensions: { key: string; name?: string; score: number; comment?: string | null }[]
}

import type { VarianceRun, PerturbationRun, PerturbationTransform } from '../types'

export interface VarianceCreateInput {
  source_task_id?: string
  spec?: { title: string; description?: string; reference_answer?: string }
  n?: number
  parallel?: boolean
  cost_cap_usd?: number
  template_id?: string
}

// Variance / Robustness Harness (E-11)
export const varianceApi = {
  create: (body: VarianceCreateInput) =>
    request<VarianceRun>(`/quality/variance`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  get: (runId: string) => request<VarianceRun>(`/quality/variance/${runId}`),
  listForTask: (taskId: string) =>
    request<VarianceRun[]>(`/quality/variance?source_task_id=${taskId}`),
}

export interface PerturbationCreateInput {
  source_task_id: string
  transforms?: PerturbationTransform[]
  variants_per_transform?: number
  base_n?: number
  parallel?: boolean
  cost_cap_usd?: number
  template_id?: string
}

// Adversarial / Perturbation Judge (E-12)
export const perturbationApi = {
  create: (body: PerturbationCreateInput) =>
    request<PerturbationRun>(`/quality/perturbation`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  get: (runId: string) => request<PerturbationRun>(`/quality/perturbation/${runId}`),
  listForTask: (taskId: string) =>
    request<PerturbationRun[]>(`/quality/perturbation?source_task_id=${taskId}`),
}

// Knowledge
export interface KnowledgeDocument {
  id: string
  filename: string
  chunk_count: number
  created_at: string
}

export interface KnowledgeSearchResult {
  text: string
  filename: string
  score: number
}

export const knowledgeApi = {
  reset: () =>
    request<{ docs_deleted: number; s3_objects_removed: number; memory_entities_deleted?: number }>(
      '/knowledge/reset', { method: 'POST' },
    ),
  getRules: () => request<{ content: string }>('/knowledge/rules'),
  putRules: (content: string) =>
    request<{ status: string }>('/knowledge/rules', {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),
  getMemory: () => request<{ content: string }>('/knowledge/memory'),
  putMemory: (content: string) =>
    request<{ status: string }>('/knowledge/memory', {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),
  listDocuments: () => request<KnowledgeDocument[]>('/knowledge/documents'),
  uploadDocument: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ id: string; filename: string; chunk_count: number }>(
      '/knowledge/documents',
      { method: 'POST', body: fd },
    )
  },
  deleteDocument: (id: string) =>
    request<{ status: string }>(`/knowledge/documents/${id}`, { method: 'DELETE' }),
  search: (query: string, limit = 5) =>
    request<{ results: KnowledgeSearchResult[] }>('/knowledge/search', {
      method: 'POST',
      body: JSON.stringify({ query, limit }),
    }),
}

// Chat
export interface ChatMessage {
  id?: string
  role: 'user' | 'assistant'
  content: string
  created_at?: string
}

export const chatApi = {
  history: (limit = 50) => request<ChatMessage[]>(`/chat/history?limit=${limit}`),
}

// Memory
import type { MemoryEntity, MemoryEntityDetail, MemoryRelation } from '../types'

export const memoryApi = {
  listEntities: (params?: { type?: string; search?: string; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params) {
      Object.entries(params).forEach(([k, v]) => { if (v != null) qs.set(k, String(v)) })
    }
    const qstr = qs.toString()
    return request<MemoryEntity[]>(`/memory/entities${qstr ? `?${qstr}` : ''}`)
  },
  getEntity: (id: string) => request<MemoryEntityDetail>(`/memory/entities/${id}`),
  createEntity: (data: { type: string; name: string; attributes?: Record<string, unknown> }) =>
    request<MemoryEntity>('/memory/entities', { method: 'POST', body: JSON.stringify(data) }),
  updateEntity: (id: string, data: Partial<MemoryEntity>) =>
    request<MemoryEntity>(`/memory/entities/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteEntity: (id: string) => request<void>(`/memory/entities/${id}`, { method: 'DELETE' }),
  listRelations: (params?: { from_id?: string; to_id?: string; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params) {
      Object.entries(params).forEach(([k, v]) => { if (v != null) qs.set(k, String(v)) })
    }
    const qstr = qs.toString()
    return request<MemoryRelation[]>(`/memory/relations${qstr ? `?${qstr}` : ''}`)
  },
  createRelation: (data: { from_id: string; to_id: string; relation_type: string; attributes?: Record<string, unknown> }) =>
    request<MemoryRelation>('/memory/relations', { method: 'POST', body: JSON.stringify(data) }),
  deleteRelation: (id: string) => request<void>(`/memory/relations/${id}`, { method: 'DELETE' }),
  extract: (taskId: string) =>
    request<{ status: string; task_id: string }>(
      `/memory/extract?task_id=${encodeURIComponent(taskId)}`,
      { method: 'POST' },
    ),
}

// Analytics
export interface TemplateAnalytics {
  template_id: string
  template_name: string
  task_count: number
  approval_rate: number
  retry_rate: number
  failure_rate: number
  avg_time_seconds: number
  avg_input_tokens: number
  avg_output_tokens: number
  total_cost_usd: number
  cost_per_task_usd: number
}

export interface TimelinePoint {
  date: string | null
  task_count: number
  total_cost_usd: number
  total_tokens: number
}

export interface ModelAnalytics {
  model: string
  task_count: number
  total_cost_usd: number
  avg_input_tokens: number
  avg_output_tokens: number
}

function analyticsQs(params: Record<string, string | number | undefined>): string {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => { if (v != null && v !== '') qs.set(k, String(v)) })
  const s = qs.toString()
  return s ? `?${s}` : ''
}

export const analyticsApi = {
  templates: (params: { period?: string; from_dt?: string; to_dt?: string } = {}) =>
    request<TemplateAnalytics[]>(`/analytics/templates${analyticsQs(params)}`),
  timeline: (params: { days?: number } = {}) =>
    request<TimelinePoint[]>(`/analytics/timeline${analyticsQs(params)}`),
  models: (params: { period?: string } = {}) =>
    request<ModelAnalytics[]>(`/analytics/models${analyticsQs(params)}`),
}

// Health
export const healthApi = {
  check: () => request<HealthStatus>('/health'),
}

// WebSocket helper — adds ?token=&workspace_id= to authenticated WS endpoints.
export function buildWsUrl(path: string): string {
  const { token, workspaceId } = useAuth.getState()
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = new URL(`${proto}//${window.location.host}${path}`)
  if (token) url.searchParams.set('token', token)
  if (workspaceId) url.searchParams.set('workspace_id', workspaceId)
  return url.toString()
}

// Types (local to API layer)
import type { Task as Task, Template } from '@/types'

interface Agent {
  container_id: string
  name: string
  status: string
  task_id: string
  template_id: string
  template_name: string
  created: string
}

interface AgentEvent {
  id: number
  task_id?: string | null
  agent_container_id?: string | null
  event_type: string
  source: string
  data: Record<string, unknown>
  created_at: string
}

interface HealthStatus {
  status: string
  version: string
  services: Record<string, string>
}
