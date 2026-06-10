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
  canonical_trajectory?: CanonicalTrajectory | null
  capability_spec?: CapabilitySpec | null
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

// Human Feedback Collection (E-05). Bands: bad 1-3 / improve 4-7 / good 8-10.
export type FeedbackBand = 'bad' | 'improve' | 'good'
export type FeedbackVerdict = 'approve' | 'reject'

export interface HumanFeedbackDimension {
  key: string
  name: string
  score: number
  band: FeedbackBand
  comment?: string | null
  judge_score?: number | null
}

export interface HumanFeedback {
  schema_version: number
  verdict: FeedbackVerdict | null
  overall_comment?: string | null
  dimensions: HumanFeedbackDimension[]
  submitted_by: string
  submitted_at: string
}

// Trace Cleaner (E-06): compact, judge-ready trajectory feeding the trajectory judge (E-07).
export type CleanedTraceStepKind = 'reasoning' | 'tool' | 'agent'

export interface CleanedTraceStep {
  seq: number
  kind: CleanedTraceStepKind
  tool_name?: string | null
  content: string
  truncated: boolean
  original_tokens: number
  kept_tokens: number
}

export interface CleanedTraceStats {
  original_tokens: number
  cleaned_tokens: number
  savings_tokens: number
  savings_pct: number
  steps_total: number
  steps_truncated: number
  events_dropped: number
}

export interface CleanedTrace {
  schema_version: number
  task: { id: string; title?: string | null; description?: string | null }
  steps: CleanedTraceStep[]
  stats: CleanedTraceStats
  config: { tool_output_token_cap: number; keep_tail_on_error: boolean }
  generated_at: string
  error?: string
}

// 6-axis Trajectory Judge (E-07): scores HOW the agent reached its result.
export type TrajectoryStatus = 'scored' | 'skipped' | 'error'

export interface TrajectoryAxis {
  key: string
  name: string
  score: number
  reason: string
}

export interface TrajectoryProfile {
  schema_version: number
  status: TrajectoryStatus
  axes: TrajectoryAxis[]
  overall_score: number | null
  loop_detected: boolean
  summary: string
  judge_model: string
  judge_input_tokens: number
  judge_output_tokens: number
  judge_cost_usd: number
  input_capped: boolean
  trace_stats: {
    original_tokens: number | null
    cleaned_tokens: number | null
    steps_total: number | null
  }
  evaluated_at: string
  errors: { error: string }[]
}

// TRACE Evidence Bank Judge (E-08): per-step judging with an accumulating
// evidence bank, then an evidence-aware 6-axis profile + a groundedness signal.
export interface EvidenceStep {
  seq: number
  kind?: string | null
  tool_name?: string | null
  redundant: boolean
  grounded: boolean | null
  assessed?: boolean
  progress: number
  execution: number
  facts: string[]
  note: string
  error?: string
}

export interface TrajectoryEvidenceProfile {
  schema_version: number
  status: TrajectoryStatus
  axes: TrajectoryAxis[]
  overall_score: number | null
  loop_detected: boolean
  summary: string
  groundedness: number | null
  redundant_steps: number
  evidence_bank: EvidenceStep[]
  judge_model: string
  judge_calls: number
  judge_input_tokens: number
  judge_output_tokens: number
  judge_cost_usd: number
  input_capped: boolean
  trace_stats: {
    original_tokens: number | null
    cleaned_tokens: number | null
    steps_total: number | null
    steps_assessed: number | null
  }
  evaluated_at: string
  errors: { seq?: number; error: string }[]
}

// Trajectory Matching (E-09): deterministic comparison of the actual tool-call
// sequence against a canonical (gold) trajectory. A bare list is a linear chain;
// {nodes, edges} is a DAG. Only applies to tasks with a canonical_trajectory.
export type CanonicalTrajectory =
  | string[]
  | {
      sequence?: string[]
      nodes?: { id?: string; tool: string }[]
      edges?: [string, string][]
      match_mode?: 'exact' | 'edit' | 'dag'
      match_threshold?: number
    }

export interface TrajectoryMatchProfile {
  schema_version: number
  status: TrajectoryStatus
  mode: 'exact' | 'edit' | 'dag'
  score: number | null
  matched: boolean
  threshold: number | null
  metrics: { exact: number; edit: number; dag: number }
  actual_sequence: string[]
  reference_sequence: string[]
  reference_form: 'sequence' | 'dag' | null
  detail: string
  trace_stats: {
    steps_total: number | null
    tool_steps: number | null
  }
  evaluated_at: string
  errors: { error: string }[]
}

// Capability-isolation Tests (E-13, part A)
export interface CapabilitySpec {
  required_tools: string[]
  category?: string | null
  match?: 'all' | 'any'
}

export type CapabilityClassification =
  | 'genuine'
  | 'cheated'
  | 'failed_with_tool'
  | 'failed_no_tool'

export interface CapabilityProfile {
  schema_version: number
  status: 'scored' | 'error'
  category: string | null
  required_tools: string[]
  match: 'all' | 'any'
  tools_called?: string[]
  tool_used?: boolean
  missing_tools?: string[]
  outcome_correct?: boolean
  outcome_signal?: 'judge' | 'reference' | 'none'
  outcome_score?: number | null
  outcome_threshold?: number
  classification?: CapabilityClassification
  capability_passed?: boolean
  trace_stats?: { steps_total: number | null; tool_steps: number | null }
  evaluated_at: string
  errors: { error: string }[]
}

export interface CapabilityCounts {
  genuine: number
  cheated: number
  failed_with_tool: number
  failed_no_tool: number
  total: number
  capability_score: number | null
}

export interface CapabilityAggregate extends CapabilityCounts {
  workspace_id: string
  filters: { category: string | null; model_used: string | null; template_id: string | null }
  by_category: Record<string, CapabilityCounts>
  by_model: Record<string, CapabilityCounts>
  by_template: Record<string, CapabilityCounts>
}

// Failure Mode Classifier (E-14): a multi-label set of failure classes (with
// confidence + reason) over the trajectory, written to `failure_profile`.
export type FailureClass =
  | 'tool_confusion'
  | 'parameter_blind'
  | 'loop'
  | 'premature_stop'
  | 'hallucinated_tool_result'
  | 'ignored_error'

export interface FailureLabel {
  class: FailureClass
  confidence: number
  reason: string
}

export interface FailureProfile {
  schema_version: number
  status: TrajectoryStatus
  failures: FailureLabel[]
  summary: string
  judge_model: string
  judge_input_tokens: number
  judge_output_tokens: number
  judge_cost_usd: number
  input_capped: boolean
  used_outcome_profile: boolean
  used_trajectory_profile: boolean
  trace_stats: {
    original_tokens: number | null
    cleaned_tokens: number | null
    steps_total: number | null
  }
  evaluated_at: string
  errors: { error: string }[]
}

// Hallucination Detection (E-15): a fact-check of the deliverable across four
// categories, each with checked/hallucinated counts and the flagged items.
export type HallucinationCategory = 'urls' | 'apis' | 'numbers' | 'citations'

export interface HallucinationItem {
  value?: string
  claim?: string
  kind: 'deterministic' | 'llm'
  supported: boolean
  reason: string
  confidence?: number
}

export interface HallucinationCategoryBlock {
  checked: number
  hallucinated: number
  items: HallucinationItem[]
}

export interface HallucinationProfile {
  schema_version: number
  status: 'scored' | 'error'
  categories: Record<HallucinationCategory, HallucinationCategoryBlock>
  hallucination_count: number
  items_total: number
  hallucination_rate: number
  summary: string
  judge_model: string
  judge_input_tokens: number
  judge_output_tokens: number
  judge_cost_usd: number
  input_capped: boolean
  used_outcome_profile: boolean
  used_trajectory_evidence: boolean
  trace_stats: {
    original_tokens: number | null
    cleaned_tokens: number | null
    steps_total: number | null
  }
  evaluated_at: string
  errors: { error: string }[]
}

export interface CalibrationProfile {
  schema_version: number
  status: 'scored' | 'error'
  predicted_confidence: number | null
  actual_correct: boolean
  outcome_signal: 'reference' | 'judge' | 'none'
  outcome_score: number | null
  outcome_threshold: number
  brier_term: number | null
  confidence_source: string
  probe_model: string
  reasoning: string
  judge_input_tokens: number
  judge_output_tokens: number
  judge_cost_usd: number
  input_capped: boolean
  used_outcome_profile: boolean
  trace_stats: {
    original_tokens: number | null
    cleaned_tokens: number | null
    steps_total: number | null
  }
  evaluated_at: string
  errors: { error: string }[]
}

export interface ReliabilityBucket {
  lo: number
  hi: number
  count: number
  avg_confidence: number | null
  accuracy: number | null
}

export interface CalibrationMetrics {
  count: number
  ece: number | null
  brier: number | null
  accuracy: number | null
  avg_confidence: number | null
  overconfidence: number | null
  reliability: ReliabilityBucket[]
}

export interface CalibrationAggregate {
  workspace_id: string
  filters: Record<string, string | null>
  bins: number
  overall: CalibrationMetrics
  by_model: Record<string, CalibrationMetrics>
  by_template: Record<string, CalibrationMetrics>
  recommendations: string[]
}

// Judge Calibration Protocol (E-17): judge-vs-human agreement per dimension.
export interface JudgeCalibrationDimension {
  key: string
  name: string
  n: number
  pearson: number | null
  spearman: number | null
  cohen_kappa: number | null
  mean_bias: number | null
  reliable: boolean
  status: 'ok' | 'insufficient_data'
}

export interface JudgeCalibrationMetrics {
  threshold_kappa: number
  sample_size: number
  n_records: number
  n_humans: number
  n_dimensions: number
  dimensions: JudgeCalibrationDimension[]
  overall: {
    n: number
    cohen_kappa: number | null
    agreement_pct: number | null
    reliable: boolean
  }
  recommendations: string[]
}

export interface JudgeCalibration {
  id: string
  workspace_id: string
  judge_config_key: string
  judge_model: string | null
  version: number
  sample_size: number
  n_dimensions: number
  threshold_kappa: number
  passed: boolean
  filters: Record<string, string | null>
  created_by: string
  created_at: string | null
  metrics: JudgeCalibrationMetrics
}

export interface JudgeCalibrationBadge {
  calibrated: boolean
  n_humans?: number
  sample_size?: number
  overall_kappa?: number | null
  judge_config_key?: string
  version?: number
  passed?: boolean
  created_at?: string | null
}

// Bias Mitigation Toolkit (E-18): controlled A/B re-judge, before vs after.
export interface BiasDimensionDelta {
  key: string
  name: string
  cohen_kappa_before: number | null
  cohen_kappa_after: number | null
  pearson_before: number | null
  pearson_after: number | null
  mean_bias_before: number | null
  mean_bias_after: number | null
  improved: boolean
}

export interface BiasReportMetrics {
  schema_version: number
  status: 'ok' | 'empty' | 'insufficient_data' | 'no_judge_model'
  threshold_kappa: number
  n_records: number
  sample_size: number
  n_dimensions: number
  toggles_requested: Record<string, boolean>
  // before/after reuse the E-17 metrics shape (per-dimension agreement + overall).
  before: JudgeCalibrationMetrics | null
  after: JudgeCalibrationMetrics | null
  dimensions_delta: BiasDimensionDelta[]
  overall_delta: {
    cohen_kappa_before: number | null
    cohen_kappa_after: number | null
    agreement_pct_before: number | null
    agreement_pct_after: number | null
    improved: boolean
  } | null
  diagnostics: {
    verbosity: {
      judge_corr_off?: number | null
      judge_corr_on?: number | null
      human_corr?: number | null
      improved?: boolean
      status: string
    }
    score_clustering: {
      spread_off?: number | null
      spread_on?: number | null
      pct_in_7_8_off?: number | null
      pct_in_7_8_on?: number | null
      clustered_off?: boolean | null
      improved?: boolean
      status: string
    }
    self_preference: {
      flagged?: boolean
      judge_model?: string | null
      agent_models?: string[]
      n_self_judged?: number
      auto_swap?: boolean
      warning?: string | null
      status: string
    }
    position_bias: { status: string; reason: string }
  }
  task_errors?: { task_id: string; error: string }[]
}

export interface BiasReport {
  id: string
  workspace_id: string
  judge_config_key: string
  judge_model: string | null
  version: number
  sample_size: number
  n_dimensions: number
  threshold_kappa: number
  passed: boolean
  filters: Record<string, string | null>
  created_by: string
  created_at: string | null
  metrics: BiasReportMetrics
}

// Aggregation Engine (E-19): Bradley-Terry / Elo leaderboard from pairwise matches.
export interface RankingPlayer {
  player: string
  rating: number
  ci_low: number
  ci_high: number
  rank: number
  wins: number
  losses: number
  ties: number
  n_matches: number
  win_rate: number | null
}

export interface RankingMetrics {
  schema_version: number
  method: 'bt' | 'elo'
  status: 'ok' | 'empty' | 'insufficient_data'
  subject: 'model' | 'template'
  source: 'derived' | 'explicit'
  n_matches: number
  n_players: number
  players: RankingPlayer[]
  params: {
    method: string
    n_resamples: number
    seed: number
    tie_epsilon: number | null
    k?: number
    passes?: number
    prior?: number
  }
  derivation?: {
    subject: string
    n_cases: number
    n_records_used: number
    n_unmatched: number
    n_players: number
    epsilon: number
  }
}

export interface RankingReport {
  id: string
  workspace_id: string
  ranking_key: string
  subject: 'model' | 'template'
  method: 'bt' | 'elo'
  version: number
  n_players: number
  n_matches: number
  passed: boolean
  filters: Record<string, string | null>
  created_by: string
  created_at: string | null
  metrics: RankingMetrics
}

export interface RankingBadge {
  ranked: boolean
  ranking_key?: string
  subject?: string
  method?: string
  version?: number
  n_players?: number
  n_matches?: number
  status?: string
  top_player?: string | null
  created_at?: string | null
}

// Reproducibility Snapshot (E-20) — per-record experiment_snapshot in
// quality_records.reproducibility. Large text is hashed in `determinism`
// (the fingerprinted core) and kept raw-capped in `content`.
export interface ExperimentSnapshot {
  schema_version: number
  captured_at: string
  determinism: {
    model_api_name: string | null
    temperature: number | null
    seed: number | null
    template_id: string | null
    template_name: string | null
    tools: string[]
    mcp_servers: string[]
    soul_md_sha256: string | null
    memory_context_sha256: string | null
    flat_memory_sha256: Record<string, string | null>
    rag: { collection: string; memory_context_present: boolean; vector_capture: string }
    tool_versions: Record<string, string | null>
    task_input: {
      title: string | null
      description_sha256: string | null
      reference_answer_sha256: string | null
      canonical_trajectory_sha256: string | null
    }
  }
  content: {
    soul_md: string
    memory_context: string
    flat_memory: Record<string, string>
    task_input: { description: string | null; reference_answer: string | null; canonical_trajectory: unknown }
  }
  manifest: { captured: string[]; missing: string[]; notes: Record<string, string> }
  fingerprint: string
}

export interface SnapshotDiff {
  fingerprint_a: string
  fingerprint_b: string
  identical: boolean
  added: Record<string, unknown>
  removed: Record<string, unknown>
  changed: Record<string, { from: unknown; to: unknown }>
  summary: string
}

export interface ReplayResult {
  replay_task_id: string
  source_task_id: string
  run_config: Record<string, unknown> | null
  fingerprint: string | null
}

// Pairwise Comparison Framework (E-21) — head-to-head "A vs B" between two task
// results on a subject axis, decided by an LLM judge (position-bias mitigated) or
// a human; judged verdicts feed the E-19 ELO leaderboard.
export type PairwiseVerdict = 'a' | 'b' | 'tie'
export type ComparisonSubject = 'model' | 'template' | 'prompt'
export type ComparisonStatus = 'pending' | 'generating' | 'ready' | 'judged' | 'failed'

export interface PairwiseJudgeDetail {
  judge_model?: string
  mitigate_position?: boolean
  position_bias_detected?: boolean | null
  orders?: {
    ab?: { winner: PairwiseVerdict; reasoning: string }
    ba?: { winner: PairwiseVerdict; winner_mapped: PairwiseVerdict; reasoning: string }
  }
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
  error?: string
}

export interface PairwiseSide {
  task_id: string
  player: string | null
  title?: string
  model_used?: string | null
  status?: string
  result_summary?: string
  weighted_score?: number | null
  missing?: boolean
}

export interface PairwiseComparison {
  id: string
  workspace_id: string
  subject: ComparisonSubject
  source_task_id: string | null
  task_a_id: string | null
  task_b_id: string | null
  b_run_config: Record<string, unknown> | null
  player_a: string | null
  player_b: string | null
  status: ComparisonStatus
  judge_mode: 'llm' | 'human'
  judge_verdict: PairwiseVerdict | null
  human_verdict: PairwiseVerdict | null
  judge_detail: PairwiseJudgeDetail | null
  human_by: string | null
  human_reasoning: string | null
  cost_usd: number
  created_by: string
  created_at: string | null
  updated_at: string | null
  completed_at: string | null
  side_by_side?: { a: PairwiseSide | null; b: PairwiseSide | null }
}

export interface PairwiseAgreement {
  n: number
  agreements: number
  agreement: number | null
}

export interface PairwiseListResponse {
  comparisons: PairwiseComparison[]
  agreement: PairwiseAgreement
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

// Variance / Robustness Harness (E-11): N re-runs of one scenario, with the
// dispersion of the result measured rather than a single point estimate.
export type VarianceStatus = 'pending' | 'running' | 'done' | 'capped' | 'failed'

export interface VarianceDistribution {
  n: number
  mean?: number
  std?: number
  min?: number
  p25?: number
  p50?: number
  p75?: number
  p95?: number
  max?: number
  values: number[]
}

export interface VarianceDimension {
  key: string
  name: string
  unit: string
  available: boolean
  dist: VarianceDistribution
}

export interface VarianceToolStability {
  runs: number
  distinct_signatures: number
  modal_share: number | null
  per_tool: { tool: string; mean: number; std: number; present_in_runs: number }[]
  signatures: { tools: string[]; count: number }[]
}

export interface VarianceAggregate {
  schema_version: number
  n_requested: number
  n_executed: number
  n_success: number
  n_failed: number
  success_rate: number
  accumulated_cost_usd: number
  capped: boolean
  dimensions: VarianceDimension[]
  tool_stability: VarianceToolStability
  generated_at: string
  error?: string
}

export interface VarianceChild {
  id: string
  status: string
  cost_usd: number
  result_summary: string
}

export interface VarianceRun {
  id: string
  workspace_id: string
  source_task_id: string | null
  source_spec: { title: string; description?: string; reference_answer?: string } | null
  template_id: string | null
  n: number
  parallel: boolean
  cost_cap_usd: number | null
  status: VarianceStatus
  child_task_ids: string[]
  accumulated_cost_usd: number
  aggregate: VarianceAggregate | null
  created_at: string | null
  updated_at: string | null
  completed_at: string | null
  children?: VarianceChild[]
}

// Adversarial / Perturbation Judge (E-12): replay a scenario under input
// perturbations and compare each perturbed profile against a clean baseline.
export type PerturbationStatus = VarianceStatus
export type PerturbationTransform = 'paraphrase' | 'noise' | 'reorder' | 'inject'

export interface PerturbationTransformResult {
  key: PerturbationTransform
  n_total: number
  n_success: number
  outcome: VarianceDistribution
  robustness: number | null
  score_delta: number | null
  dimension_deltas: Record<string, number>
  injection_followed_count?: number
  injection_followed_ids?: string[]
  injection_followed_rate?: number
}

export interface PerturbationSafety {
  injection_tested: boolean
  n: number
  followed_count: number
  followed_rate: number
  injection_followed: boolean
}

export interface PerturbationAggregate {
  schema_version: number
  n_executed: number
  capped: boolean
  accumulated_cost_usd: number
  base: {
    n_total: number
    n_success: number
    outcome: VarianceDistribution
    score: number | null
    dimensions: Record<string, number>
  }
  transforms: PerturbationTransformResult[]
  overall_robustness: number | null
  robustness_available: boolean
  safety: PerturbationSafety | null
  generated_at: string
  error?: string
}

export interface PerturbationChild {
  id: string
  status: string
  cost_usd?: number
  title?: string
  result_summary?: string
  injection_followed?: boolean
}

export interface PerturbationRun {
  id: string
  workspace_id: string
  source_task_id: string | null
  template_id: string | null
  transforms: PerturbationTransform[]
  variants_per_transform: number
  base_n: number
  parallel: boolean
  cost_cap_usd: number | null
  status: PerturbationStatus
  base_task_ids: string[]
  perturbed_task_ids: Record<string, string[]>
  accumulated_cost_usd: number
  aggregate: PerturbationAggregate | null
  created_at: string | null
  updated_at: string | null
  completed_at: string | null
  base_children?: PerturbationChild[]
  perturbed_children?: Record<string, PerturbationChild[]>
}
