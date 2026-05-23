export type TaskStatus =
  | 'backlog'
  | 'ready'
  | 'decomposing'
  | 'in_progress'
  | 'review'
  | 'awaiting_approval'
  | 'done'
  | 'failed'

export type TaskPriority = 'low' | 'medium' | 'high' | 'urgent'

export interface Task {
  id: string
  parent_id?: string | null
  title: string
  description?: string | null
  status: TaskStatus
  priority: TaskPriority
  template_id?: string | null
  agent_container_id?: string | null
  result_summary?: string | null
  reference_answer?: string | null
  result_files: string[]
  token_usage: Record<string, number>
  retry_count: number
  max_retries: number
  user_feedback?: string | null
  orchestrator_feedback?: string | null
  model_used?: string | null
  cost_usd?: number | null
  depends_on?: string[] | null
  log_archive_s3_path?: string | null
  created_at: string
  updated_at: string
  started_at?: string | null
  completed_at?: string | null
  subtasks?: Task[]
}

export interface MCPServer {
  name: string
  command: string
  args: string[]
  env?: Record<string, string>
}

export interface Template {
  id: string
  name: string
  description: string
  soul_md: string
  model_id: string | null
  model_display_name: string | null
  model_api_name: string | null
  provider_name: string | null
  rubric_id: string | null
  tools: string[]
  mcp_servers: MCPServer[]
  max_ram: string
  max_cpu: number
  timeout_minutes: number
  tags: string[]
  created_at: string
  updated_at: string
}

export interface Provider {
  id: string
  name: string
  endpoint: string
  api_key_masked: string
  created_at: string
  updated_at: string
}

export interface LLMModel {
  id: string
  provider_id: string
  display_name: string
  api_name: string
  input_price_per_1m_usd: number
  output_price_per_1m_usd: number
  created_at: string
  updated_at: string
}

export interface ModelTestResponse {
  status: 'ok' | 'error'
  latency_ms?: number
  model?: string
  sample?: string
  error?: string
}

export interface SystemModels {
  orchestrator_model_id: string | null
  chat_model_id: string | null
  memory_extractor_model_id: string | null
  quality_judge_model_id: string | null
}

// Quality Rubric Engine (E-02) + Reference-based Judge (E-03)
export type EvaluatorType = 'judge' | 'objective' | 'human' | 'reference'

// Reference-based evaluation modes (E-03); pairwise is deferred.
export type ReferenceMode = 'pointwise' | 'exact' | 'fuzzy' | 'semantic'

// Objective/behavioral probes (E-04); POC scope: Python static analysis.
export type ProbeType = 'lint' | 'types'

export interface RubricDimension {
  key: string
  name: string
  description: string
  evaluator: EvaluatorType
  reference_mode?: ReferenceMode | null
  probe?: ProbeType | null
  weight: number
  threshold: number | null
  critical: boolean
}

export interface Rubric {
  id: string
  workspace_id: string
  name: string
  description: string
  applies_to: string | null
  is_default: boolean
  dimensions: RubricDimension[]
  created_at: string
  updated_at: string
}

export type DimensionStatus = 'scored' | 'deferred' | 'error' | 'skipped'

export interface QualityProfileDimension {
  key: string
  name: string
  evaluator: EvaluatorType
  reference_mode?: ReferenceMode | null
  probe?: ProbeType | null
  max: number
  weight: number | null
  threshold: number | null
  critical: boolean
  status: DimensionStatus
  score: number | null
  reasoning?: string
  passed?: boolean
  error?: string
}

export interface QualityProfile {
  schema_version: number
  rubric_id: string
  rubric_name: string
  dimensions: QualityProfileDimension[]
  weighted_score: number | null
  gate: { passed: boolean; failed_dimensions: string[] }
  judge_model: string
  judge_input_tokens: number
  judge_output_tokens: number
  judge_cost_usd: number
  evaluated_at: string
  errors: { key: string; error: string }[]
}

export interface Agent {
  container_id: string
  name: string
  status: string
  task_id: string
  template_id: string
  template_name: string
  created: string
}

export interface AgentEvent {
  id: number
  task_id?: string | null
  agent_container_id?: string | null
  event_type: string
  source: 'orchestrator' | 'agent' | 'user' | 'system'
  data: Record<string, unknown>
  created_at: string
}

export interface HealthStatus {
  status: string
  version: string
  services: Record<string, string>
}

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  backlog: 'Backlog',
  ready: 'Ready',
  decomposing: 'Decomposing',
  in_progress: 'In Progress',
  review: 'Review',
  awaiting_approval: 'Awaiting Approval',
  done: 'Done',
  failed: 'Failed',
}

export const PRIORITY_COLORS: Record<TaskPriority, string> = {
  low: 'bg-gray-100 text-gray-700',
  medium: 'bg-blue-100 text-blue-700',
  high: 'bg-orange-100 text-orange-700',
  urgent: 'bg-red-100 text-red-700',
}

export const SOURCE_COLORS: Record<string, string> = {
  orchestrator: 'bg-purple-100 text-purple-700',
  agent: 'bg-blue-100 text-blue-700',
  user: 'bg-green-100 text-green-700',
  system: 'bg-gray-100 text-gray-700',
}

export const KANBAN_COLUMNS: TaskStatus[] = [
  'backlog',
  'ready',
  'in_progress',
  'review',
  'awaiting_approval',
  'done',
  'failed',
]

export interface LogChunk {
  id: string | null
  chunk_seq: number
  content: string
  tool_name?: string | null
  created_at: string | null
}

export interface LogChunksResponse {
  archived: boolean
  archive_path: string | null
  chunks: LogChunk[]
}

export type AttemptOutcome = 'completed' | 'failed' | 'aborted' | 'running'

export interface AgentAttempt {
  agent_container_id: string
  spawned_at: string
  finished_at: string | null
  outcome: AttemptOutcome
  error: string | null
}

export interface DecompositionSubtask {
  id: string
  title: string
  template_name: string | null
  status: TaskStatus
  retry_count: number
  max_retries: number
  depends_on: string[]
  started_at: string | null
  completed_at: string | null
  cost_usd: number
  result_files_count: number
  attempts: AgentAttempt[]
}

export interface DecompositionResponse {
  parent: {
    id: string
    title: string
    status: TaskStatus
    started_at: string | null
    completed_at: string | null
    cost_usd: number
  }
  subtasks: DecompositionSubtask[]
}

export interface MemoryEntity {
  id: string
  type: string
  name: string
  attributes: Record<string, unknown>
  created_by: string
  created_at: string
  updated_at: string
}

export interface MemoryRelation {
  id: string
  from_id: string
  to_id: string
  relation_type: string
  attributes: Record<string, unknown>
  created_at: string
}

export interface MemoryEntityDetail extends MemoryEntity {
  relations: MemoryRelation[]
}
