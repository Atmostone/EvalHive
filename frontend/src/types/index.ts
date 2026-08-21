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
  /** Why this run stopped measuring the model (SPA-87). Recorded from the start,
   *  but only readable in SQL until SPA-113. */
  failure_type?: string | null
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
  // Overhead the platform incurred deciding about this task — template
  // selection, decomposition, result evaluation. Kept apart from cost_usd and
  // token_usage above, which are the AGENT's own effort (SPA-111).
  orchestrator_cost_usd?: number | null
  orchestrator_usage?: Record<string, unknown> | null
  depends_on?: string[] | null
  log_archive_s3_path?: string | null
  origin?: string | null
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

// Tool & MCP Registry (SPA-41) — a workspace-level entry templates reference by id.
export type RegistryKind = 'builtin' | 'mcp'
export interface RegistryEntry {
  id: string
  workspace_id: string
  name: string
  kind: RegistryKind
  config: Record<string, unknown>
  secrets: Record<string, string> // masked on read
  secret_keys: string[]
  enabled: boolean
  description: string | null
  created_by: string
  created_at: string | null
  updated_at: string | null
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
  tool_ids: string[]
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
  max_concurrency: number | null
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

// The append-only annotation ledger (SPA-85). `human_feedback` above is the
// materialised latest human row of this list.
export type AnnotatorType = 'human' | 'llm_judge' | 'synthetic' | 'legacy'

export interface Annotation {
  id: string
  task_id: string
  annotator_type: AnnotatorType
  annotator_id: string | null
  annotator_label: string | null
  protocol_version: number
  blind_to_model: boolean
  blind_to_judge: boolean
  /** Collected without sight of the other annotators' ratings — true for every
   *  session-produced rating, since a session never serves them. Inter-annotator
   *  κ is computed only over these. */
  blind_to_peers: boolean
  verdict: FeedbackVerdict | null
  overall_comment?: string | null
  dimensions: HumanFeedbackDimension[]
  /** What the judge had said at annotation time — frozen, so a re-judge cannot
   *  rewrite this row's calibration pair. */
  judge_observation: Record<string, unknown>
  /** The row this one replaces — set only when the same annotator re-rated. */
  supersedes_id: string | null
  /** The session that produced this rating — the evidence behind the blindness
   *  flags. Null for machine annotators and pre-session rows. */
  session_id: string | null
  created_at: string | null
  /** Another annotator's row, always served with their scores, verdict and
   *  comments removed inside an annotation session (SPA-85). Reading them would
   *  make your rating dependent on theirs, and the agreement computed over the
   *  result would measure nothing. They are visible outside the session — in
   *  `GET …/annotations` and the calibration export. */
  redacted?: boolean
}

/** One annotation session's whole payload (SPA-85): the protocol declared before
 *  anything was fetched, plus everything needed to rate the run, sanitized to
 *  match it in one place. Under a blind session the judge material is absent
 *  from the response — not hidden by the client. */
export interface AnnotationSessionBundle {
  session_id: string
  task_id: string
  protocol: {
    protocol_version: number
    blind_to_judge: boolean
    blind_to_model: boolean
    blind_to_peers: boolean
  }
  review: ReviewContext
  quality_profile: QualityProfile | null
  trajectory_profile: TrajectoryProfile | null
  human_feedback: HumanFeedback | null
  annotations: Annotation[]
  model_used: string | null
}

// Calibration queue (E-17): records carrying a judge profile, awaiting human annotation.
export interface CalibrationQueueItem {
  task_id: string
  title: string
  origin: string
  template_name?: string | null
  model_used?: string | null
  benchmark_suite?: string | null
  weighted_score?: number | null
  n_dimensions: number
  has_feedback: boolean
  created_at?: string | null
}

export interface CalibrationQueue {
  total: number
  done: number
  pending: number
  items: CalibrationQueueItem[]
}

// What an annotator sees when rating a result: the task prompt + the deliverable.
export interface ReviewFile {
  name: string
  text: string | null
  binary: boolean
  // Converted Markdown of the deliverable (SPA-71): docx/pdf/pptx/xlsx/csv/ics/
  // json rendered readably. null when unconvertible/failed — UI falls back to text.
  markdown?: string | null
}

export interface ReviewContext {
  task_id: string
  title: string
  description?: string | null
  reference_answer?: string | null
  result_summary?: string | null
  files: ReviewFile[]
}

// Trace Cleaner (E-06): compact, judge-ready trajectory feeding the trajectory judge (E-07).
// 'attempt' is a boundary between agent runs of the same task (SPA-113), not
// something the agent did — it is rendered as a separator, never numbered.
export type CleanedTraceStepKind = 'reasoning' | 'tool' | 'agent' | 'attempt'

export interface CleanedTraceStep {
  seq: number
  /** Which run of this task the step belongs to, 1-based (SPA-113). Repetition
   *  across a boundary is a retry, not a loop. */
  attempt?: number
  kind: CleanedTraceStepKind
  tool_name?: string | null
  /** The call's arguments (SPA-86). `null` means they were never recorded — not
   *  that the call took none, which is `{}`. */
  arguments?: Record<string, unknown> | null
  arguments_truncated?: boolean
  /** The call was recorded but no result ever arrived (hang / crash / raise). */
  result_missing?: boolean
  /** Output parts that never reached the backend; the gap is marked, not spliced. */
  parts_missing?: number
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
  steps_args_truncated?: number
  steps_result_missing?: number
  steps_parts_missing?: number
  events_dropped: number
  /** Progress pings removed as echoes of a tool call already recorded in full. */
  progress_echoes_dropped?: number
  /** How many times the agent was run for this task; >1 means the trace spans retries. */
  attempts?: number
}

export interface CleanedTrace {
  schema_version: number
  task: { id: string; title?: string | null; description?: string | null }
  steps: CleanedTraceStep[]
  stats: CleanedTraceStats
  /** Either cap is 0 when truncation is off entirely. */
  config: {
    tool_output_token_cap: number
    tool_args_token_cap?: number
    keep_tail_on_error: boolean
  }
  generated_at: string
  error?: string
}

/** What the judge was allowed to read, and what fitting the budget cost (SPA-86).
 *  An E-07 score from an untrimmed run and one from a trimmed run answer different
 *  questions; without this they are silently comparable. */
export interface TrajectoryTrim {
  mode: 'none' | 'budget'
  max_input_tokens: number | null
  /** The BUDGET removed something. Not the whole story — the cleaner's per-output
   *  caps run first, so read `anything_removed` before claiming nothing was lost. */
  capped: boolean
  anything_removed?: boolean
  pre_trim_outputs_truncated?: number
  pre_trim_args_truncated?: number
  pre_trim_dropped_tokens?: number
  output_cap_applied?: number | null
  /** Error-looking outputs get their own cap and are given up last. */
  error_output_cap_applied?: number | null
  reasoning_cap_applied?: number | null
  outputs_shrunk?: number
  reasoning_shrunk?: number
  steps_omitted?: number
  omitted_signatures?: string
  hard_cut_tokens?: number
  tool_output_token_cap?: number
  tool_args_token_cap?: number
  keep_tail_on_error?: boolean
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
  trim?: TrajectoryTrim
  trace_stats: {
    original_tokens: number | null
    cleaned_tokens: number | null
    steps_total: number | null
    steps_truncated?: number | null
    steps_args_truncated?: number | null
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

// Failure Mode aggregate (E-14): per-class distributions across the workspace
// with breakdowns by class / model / template (the "compare models by failure
// distribution" view). Returned by GET /quality/failure-modes/aggregate.
export interface FailureBucket {
  runs_total: number
  failure_runs: number
  by_class: Record<string, number>
  failure_rate: number | null
  rate: Record<string, number> | null
}

export interface FailureAggregate {
  workspace_id: string
  filters: { model_used: string | null; template_id: string | null; failure_class: string | null; suite: string | null }
  runs_total: number
  failure_runs: number
  failure_rate: number | null
  rate: Record<string, number> | null
  by_class: Record<string, FailureBucket>
  by_model: Record<string, FailureBucket>
  by_template: Record<string, FailureBucket>
}

// Hallucination aggregate (E-15): per-category checked/hallucinated rates across
// the workspace with breakdowns by category / model / template. Returned by
// GET /quality/hallucinations/aggregate.
export interface HallucinationCatCount {
  checked: number
  hallucinated: number
  rate: number | null
}

export interface HallucinationBucket {
  runs_total: number
  hallucinated_runs: number
  hallucinated_run_rate: number | null
  by_category: Record<string, HallucinationCatCount>
}

export interface HallucinationAggregate {
  workspace_id: string
  filters: { model_used: string | null; template_id: string | null; category: string | null; suite: string | null }
  runs_total: number
  hallucinated_runs: number
  hallucinated_run_rate: number | null
  by_category: Record<string, HallucinationBucket>
  by_model: Record<string, HallucinationBucket>
  by_template: Record<string, HallucinationBucket>
}

// Benchmark Case Store catalogue (read-only) — GET /api/benchmarks/suites[/{suite}].
export interface BenchmarkSuiteSummary {
  name: string
  n_cases: number
}

export interface BenchmarkCaseInfo {
  id: string
  title: string
  category: string | null
  family: string | null
  required_services: string[]
  mcp_servers: string[]
  gold: {
    reference_answer: boolean
    rubric: boolean
    canonical_trajectory: boolean
    capability_spec: boolean
    external_eval: boolean
  }
}

export interface BenchmarkSuiteDetail {
  suite: string
  n_cases: number
  cases: BenchmarkCaseInfo[]
}

// E-01 Data Lake — immutable execution-record corpus (GET /api/data-lake/*).
export interface DataLakeRecordSummary {
  task_id: string
  template_id: string | null
  template_name: string | null
  model_used: string | null
  final_status: string | null
  is_decomposition_root: boolean
  cost_usd: number
  input_tokens: number | null
  output_tokens: number | null
  duration_seconds: number | null
  tool_call_count: number | null
  public_dataset_opt_in: boolean
  record_s3_path: string | null
  created_at: string | null
}

export interface DataLakeGroupRow {
  group: string | null
  count: number
  avg_cost_usd: number
  avg_tokens: number
  avg_duration_s: number
  approval_rate: number
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
  cohen_kappa_ci?: KappaCI | null
  mean_bias: number | null
  reliable: boolean
  status: 'ok' | 'insufficient_data'
}

/** Agreement between annotators who rated the same run (SPA-85) — impossible to
 *  compute while a run could only carry one rating. */
export interface InterAnnotatorAgreement {
  available: boolean
  n_records: number
  n_annotators: number
  dimensions: {
    key: string
    name: string
    n: number
    cohen_kappa: number | null
    cohen_kappa_ci?: KappaCI | null
    agreement_pct: number | null
    reliable: boolean
    status: 'ok' | 'insufficient_data'
  }[]
  overall: {
    n: number
    cohen_kappa: number | null
    cohen_kappa_ci?: KappaCI | null
    agreement_pct: number | null
    reliable: boolean
  }
}

export interface JudgeCalibrationMetrics {
  threshold_kappa: number
  sample_size: number
  n_records: number
  /** People — distinct annotator ids of type `human` (SPA-85). */
  n_humans: number
  n_annotators: number
  n_annotations: number
  /** Ratings collected before the ledger; they carry no attributable person. */
  n_legacy: number
  /** Share of pairs whose judge side came from a frozen observation. */
  judge_frozen_pct: number | null
  n_dimensions: number
  dimensions: JudgeCalibrationDimension[]
  overall: {
    n: number
    cohen_kappa: number | null
    cohen_kappa_ci?: KappaCI | null
    agreement_pct: number | null
    reliable: boolean
  }
  inter_annotator: InterAnnotatorAgreement
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
  n_legacy?: number
  judge_frozen_pct?: number | null
  inter_annotator_kappa?: number | null
  inter_annotator_records?: number
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
  // The model's own deliberation preceding this step (SPA-114). Never merged into
  // `content` — «what it thought» and «what it did» are different things, and a
  // reasoning model's answer to "how did it work?" lives on this side.
  reasoning?: string | null
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

// --- Experiments (SPA-40) ---

export type ExperimentStatus =
  | 'draft'
  | 'running'
  | 'paused'
  | 'completed'
  | 'capped'
  | 'failed'
  | 'cancelled'

export interface ExperimentConfig {
  config_key: string
  label: string
  fingerprint: string
  orchestrator: boolean
  template_id?: string | null
  model_id?: string | null
  temperature?: number | null
  seed?: number | null
  soul_md?: string | null
  tools_override?: { enable?: string[]; disable?: string[] } | null
  memory_mode?: 'off' | 'flat' | 'structured' | null
  /** SPA-84: set when the configuration was retired. Its cells keep their
   *  lineage but leave the matrix, so it must be shown as retired rather than
   *  drawn as an empty column. */
  retired_at?: string | null
  /** SPA-84: what this configuration actually resolved to when the experiment
   *  started — the fingerprint covers ids, not the contents behind them. */
  resolved?: {
    resolved_at: string
    template_name?: string
    template_content_sha256?: string
    model_api_name?: string
    model_display_name?: string
    provider_name?: string
    agent_images?: Record<string, string | null>
  } | null
}

export interface Experiment {
  id: string
  name: string
  description?: string | null
  status: ExperimentStatus
  dataset: Record<string, unknown>
  n_cases: number
  n_configs: number
  /** SPA-84: bumped by every mutation; a cached report is only valid for the
   *  revision it was computed from. */
  revision: number
  n_retired_configs: number
  n_runs_per_cell: number
  total_runs: number
  budget_limit_usd?: number | null
  max_parallel?: number | null
  n_toolathlon_lanes?: number | null
  eval_config: Record<string, unknown>
  accumulated_cost_usd: number
  has_report: boolean
  error?: string | null
  created_by: string
  created_at: string
  started_at?: string | null
  completed_at?: string | null
  configurations?: ExperimentConfig[]
  dataset_cases?: { case_key: string; title: string }[]
  matrix_spec?: Record<string, unknown>
}

export interface ExperimentMatrixCell {
  config_key: string
  case_key: string
  counts: Record<string, number>
  /** SPA-87: runs in this cell whose outcome infrastructure decided. They are
   *  still counted in `counts` — the matrix is a progress view — but contribute
   *  to none of the means below. */
  contaminated?: number
  quality_mean?: number | null
  trajectory_mean?: number | null
  // Spread across the cell's scored runs (population σ; null until ≥2 samples) —
  // a stable score vs a noisy one (SPA-73).
  quality_std?: number | null
  trajectory_std?: number | null
  human_std?: number | null
  // Per-cell rubric dimension / trajectory axis means, sorted worst-first — which
  // dimension/axis drags this config×case (cell tooltip, SPA-73).
  dim_means?: { name: string; mean: number }[]
  axis_means?: { name: string; mean: number }[]
  // Toolathlon executable verdict tally for the cell (gold.external_eval).
  external_pass?: number
  external_total?: number
  // E-05 human annotation aggregate for the cell (the third oracle).
  human_mean?: number | null
  human_rated?: number
  human_approve?: number
}

export interface ExperimentDetail extends Experiment {
  configurations: ExperimentConfig[]
  dataset_cases: { case_key: string; title: string }[]
  matrix: ExperimentMatrixCell[]
  run_totals: Record<string, number>
}

export interface ExperimentPreview {
  n_configs: number
  n_cases: number
  n_runs_per_cell: number
  total_runs: number
  est_cost_usd: number
  est_duration_minutes: number
  warnings: string[]
}

export interface ExperimentRunResult {
  config_key: string
  case_key: string
  run_index: number
  status: string
  /** SPA-84: executions this cell has had. >1 means earlier attempts are in the
   *  ledger and this row is the survivor, not the first result. */
  attempt_count?: number
  retired_at?: string | null
  task_id?: string | null
  task_status?: string | null
  result_summary?: string | null
  /** SPA-87: why the run died, and whether that means infrastructure rather than
   *  the model decided it. Raw rows are never filtered — this is the ledger — so
   *  the mark is how a consumer tells a quota casualty from a weak result. */
  failure_type?: string | null
  contaminated?: boolean
  external_verdict?: 'pass' | 'fail' | null
  weighted_score?: number | null
  trajectory_score?: number | null
  cost_usd: number
  duration_seconds?: number | null
  quality_profile?: QualityProfile | null
  trajectory_profile?: TrajectoryProfile | null
  repro_fingerprint?: string | null
  completed_at?: string | null
}

export interface ExperimentConfigSummary {
  config_key: string
  label: string
  n_runs: number
  success_rate?: number | null
  success_rate_ci?: ProportionCI | null
  quality_mean?: number | null
  trajectory_mean?: number | null
  cost_mean?: number | null
  duration_mean?: number | null
  // SPA-77: confound-controlled effort — tokens (primary), steps, and the
  // difficulty-normalized relative effort (tokens ÷ per-case median across configs).
  tokens_mean?: number | null
  n_tokens?: number
  steps_mean?: number | null
  rel_effort?: number | null
}

// SPA-77: per-config effort row (token/$ effort, difficulty-normalized).
export interface ExperimentEffortRow {
  config_key: string
  label: string
  tokens_mean?: number | null
  // SPA-114: a SUBSET of the output tokens, not an addition. null = this config
  // reported no split at all (a non-reasoning model, or a provider that does not
  // say) — which is not the same as a share of zero.
  reasoning_tokens_mean?: number | null
  reasoning_share?: number | null  // of OUTPUT tokens, not of input+output
  steps_mean?: number | null
  cost_mean?: number | null
  duration_mean?: number | null  // wall-clock — caveated secondary (throttling/waits)
  rel_effort?: number | null     // ×median; 1.0 = typical difficulty-adjusted effort
  n: number
}

// Per-config (or experiment-total) cost decomposition in USD (SPA-73).
export interface ExperimentCostRow {
  config_key?: string
  label?: string
  agent: number
  // The orchestrator's own decision calls (SPA-111). Zero on any run recorded
  // before it was metered — see `orchestrator_metered` on the breakdown.
  orchestrator: number
  judge_outcome: number
  judge_trajectory: number
  judge_evidence: number
  judge_failure: number
  judge_hallucination: number
  judge_total: number
  total: number
}

// Executable-checker (E-23) detail for a run: verdict + container log tails.
export interface ExternalCheckerLogs {
  task_id: string
  available: boolean
  verdict?: 'pass' | 'fail' | null
  case_key?: string
  config_key?: string
  label?: string | null
  launch_time?: string | null
  eval_log?: string | null
  preprocess_log?: string | null
}

export interface OrchestratorSide {
  configs: string[]
  n_runs: number
  success_rate?: number | null
  quality_mean?: number | null
  trajectory_mean?: number | null
  cost_mean?: number | null
  duration_mean?: number | null
  tokens_mean?: number | null
}

export interface ExperimentRq2Cell {
  n: number
  cells: { pass_high: number; pass_low: number; fail_high: number; fail_low: number }
  agreement?: number | null
  agreement_ci?: ProportionCI | null
  /** `fail_high` as a rate: work the checker failed and the judge scored high. */
  over_credit_rate?: number | null
  over_credit_ci?: ProportionCI | null
}

/** SPA-87: the judge↔checker relationship WITHOUT a threshold — the judge's score
 *  distribution split by the executable verdict, plus AUC. `median_on_fail` is the
 *  over-credit number; nothing here moves when someone picks a different cut-off. */
export interface ExperimentJudgeDiscriminationCell {
  n_checker_pass: number
  n_checker_fail: number
  median_on_pass?: number | null
  median_on_fail?: number | null
  mean_on_pass?: number | null
  mean_on_fail?: number | null
  separation?: number | null
  auc?: number | null
  auc_ci?: { auc: number; lo: number; hi: number; n_positive: number; n_negative: number } | null
  mann_whitney?: { u: number; z: number; p: number; approx: boolean } | null
}

/** SPA-84: a configuration whose frozen resolution no longer matches reality —
 *  the template behind it was edited, the model row repointed, or the agent image
 *  rebuilt. All of these change what a condition MEANS at an unchanged fingerprint. */
export interface ConfigDrift {
  config_key: string
  label?: string
  resolved_at?: string
  changed: Record<string, { pinned: string | null; current: string | null }>
  /** Cases whose runs did NOT all execute under the same condition, recorded per
   *  run at spawn: `{case_key: [fingerprint, ...]}`. Grouped per case because the
   *  resolved tool set legitimately differs between cases. The stronger signal —
   *  `changed` compares the pin against now and misses an edit reverted before
   *  the report. */
  split_cases?: Record<string, string[]>
  /** Distinct case-independent conditions across the WHOLE configuration —
   *  model, prompt, image, limits. This is the one that catches an edit made
   *  between two cases: with the default single run per cell, a per-case
   *  comparison has exactly one value and can never disagree. */
  core_conditions?: string[]
}

// SPA-88 trust taxonomy. What a status may DRIVE is the whole point of the split:
// 'reliable_absolute' and 'moderate_agreement' may carry numeric aggregates;
// 'rank_only' is a scale-shifted judge — ordering only, never a mean; the rest
// carry nothing. 'insufficient' (too few pairs) and 'unreliable' (the judge
// disagrees) used to be the same amber light, and they are not the same claim.
export type AxisTrustStatus =
  | 'reliable_absolute'
  | 'moderate_agreement'
  | 'rank_only'
  | 'insufficient'
  | 'unreliable'
  | 'not_calibrated'

export interface ExperimentTrustAxis {
  key: string
  name: string
  status: AxisTrustStatus
  source?: string | null
  kappa?: number | null
  /** The gate classifies by the point estimate; the interval says how firmly.
   *  Carried, not acted on — where the cut-offs sit was decided in SPA-88. */
  kappa_ci?: KappaCI | null
  rho?: number | null
  n?: number
}

/** Bootstrap interval for Cohen's κ. The reliability gate classifies by the point
 *  estimate; this says how firmly it stands there (SPA-62). */
export interface KappaCI {
  kappa: number
  lo: number
  hi: number
  n: number
  alpha: number
  n_resamples: number
}

/** Percentile-bootstrap interval for a difference of means. */
export interface DiffCI {
  mean_diff: number
  lo: number
  hi: number
  alpha: number
  n_resamples: number
}

/** Wilson score interval for a proportion — used wherever a rate is reported. */
export interface ProportionCI {
  p: number
  lo: number
  hi: number
  n: number
  alpha: number
}

export interface ExperimentSignificanceRow {
  a: string
  b: string
  metric: string
  p: number
  /** Benjamini-Hochberg q within this row's family. `significant` follows THIS. */
  q?: number
  significant: boolean
  /** What the star would have said before the correction — kept so a demotion is
   *  legible instead of a row that quietly stopped being green. */
  significant_uncorrected?: boolean
  /** Headline metric (corrected among a handful) vs rubric-dimension screen
   *  (corrected among dozens). Families are corrected separately. */
  family?: 'confirmatory' | 'exploratory'
  /** The matrix runs the same cases across configs, so the comparison is paired
   *  whenever the two sides share enough of them; Welch is the fallback. */
  design?: 'paired' | 'unpaired'
  unpaired_reason?: 'insufficient_shared_cases' | null
  /** `sign` = the paired test that survives zero variance / too few non-zero
   *  pairs; `identical` = the two configs scored the same on every shared case,
   *  which defeats all three paired tests and is the most definite answer there
   *  is rather than the absence of one. */
  primary_test?: 'paired_t' | 'wilcoxon' | 'sign' | 'identical' | 'welch' | 'mann_whitney'
  /** Non-null names a magnitude this row is not licensed to make — a rank-rescued
   *  axis agrees with the human on ORDER only, so effect, ci, power and
   *  equivalence are all withheld rather than merely absent (SPA-115). */
  magnitudes_withheld?: 'rank_only_axis' | null
  n_pairs?: number
  n_cases_a?: number
  n_cases_b?: number
  /** Cases only one config finished — case-level survivor conditioning, named. */
  unpaired_cases?: { a: string[]; b: string[] }
  /** d_z on a paired row, Hedges' g on an unpaired one. Different estimands: they
   *  are standardised by different spreads and must never be compared. */
  effect?: number | null
  effect_kind?: 'cohens_dz' | 'hedges_g'
  ci?: DiffCI | null
  /** TOST: whether «no difference» is a claim or an absence of one. */
  equivalence?:
    | { margin: number; p: number; equivalent: boolean; n_pairs: number; exact: boolean }
    | { available: false; reason: string }
    | null
  /** What this design could have seen: the smallest detectable difference at 80%
   *  power, and the n the observed difference would have needed. */
  power?: {
    sd: number
    observed_diff: number
    mde: number
    n_required: number | null
    n: number
    alpha: number
    power: number
    paired: boolean
  } | null
  // A rank-rescued axis cannot support a comparison of means, so Welch is not run
  // at all and the verdict rests on Mann-Whitney — approximate, hence a weaker claim.
  rank_only?: boolean
  paired_t?: { t: number; df: number; p: number; mean_diff: number; n_pairs: number } | null
  wilcoxon?: { w: number; z: number; p: number; n_pairs: number; approx: boolean } | null
  welch?: { t: number; df: number; p: number; mean_a: number; mean_b: number } | null
  mann_whitney?: { u: number; z: number; p: number; approx: boolean } | null
  // The axis this row was measured through, and how far it is trusted.
  axis?: {
    kind: 'outcome_axis' | 'outcome_aggregate' | 'trajectory_aggregate'
    key?: string | null
    name?: string
    status?: AxisTrustStatus | null
    numeric: boolean
    rank: boolean
    n_axes_numeric?: number
    n_axes?: number
  }
}

export interface ExperimentTrustedView {
  available: boolean
  policy: {
    numeric_statuses: AxisTrustStatus[]
    rank_statuses: AxisTrustStatus[]
    reliable_kappa: number
    moderate_kappa: number
    rank_rho: number
    min_samples: number
  }
  outcome_axes: ExperimentTrustSplit
  trajectory_axes: ExperimentTrustSplit
  summary: {
    per_config: {
      config_key: string
      label: string
      quality_mean?: number | null
      n_quality: number
      trajectory_mean?: number | null
      n_trajectory: number
    }[]
  }
  pareto: ExperimentReport['pareto']
  leaderboard: ExperimentReport['leaderboard'] & { basis?: string }
  significance: ExperimentSignificanceRow[]
  significance_correction: ExperimentSignificanceCorrection
  // What the gate cost, in the only currency the reader cares about: conclusions.
  dropped: {
    significance_rows: number
    significant_rows: number
    significant_metrics: string[]
    demoted_rows: number
    demoted_metrics: string[]
  }
}

/** How many tests the table ran, and what it did about that. */
export interface ExperimentSignificanceCorrection {
  method: 'benjamini_hochberg'
  controls: 'fdr'
  alpha: number
  n_tests: number
  families: Record<string, {
    n_tests: number
    n_significant: number
    n_significant_uncorrected: number
  }>
  equivalence_margin: number | null
  /** Fewer shared cases than this and nothing is testable at all. */
  min_cases: number
  n_omitted: number
  omitted: Record<string, number>
}

/** The population the report's numbers are about — stated, because it is not the
 *  one a reader assumes. */
export interface ExperimentEstimand {
  population: 'success_runs'
  unit: 'case_cell_mean'
  quantity: string
  survivor_conditioned: boolean
  excluded_by_status: Record<string, number>
  equivalence_margin: number | null
  margin_source: 'pre_registered' | 'default'
}

export interface ExperimentTrustSplit {
  numeric: ExperimentTrustAxis[]
  rank_only: ExperimentTrustAxis[]
  excluded: ExperimentTrustAxis[]
  n_axes: number
}

export interface ExperimentReport {
  schema_version: number
  generated_at: string
  partial: boolean
  n_terminal_runs: number
  /** SPA-84: which experiment revision this report was computed from. */
  input_revision?: number
  input_fingerprint?: string
  selection?: 'latest_valid' | 'all_attempts' | 'first_attempt'
  config_drift?: ConfigDrift[]
  summary: {
    total_runs: number
    success: number
    failed: number
    skipped: number
    /** SPA-87: runs infrastructure decided the outcome of, removed from every
     *  aggregate above. Non-zero means success_rate answers a different question
     *  — see success_rate_basis. */
    excluded_contaminated?: number
    success_rate_basis?: 'settled' | 'settled_non_contaminated'
    accumulated_cost_usd: number
    budget_limit_usd?: number | null
    per_config: ExperimentConfigSummary[]
  }
  /** SPA-87: what was dropped and why, so an exclusion is a reported act. */
  exclusions?: {
    contaminated: number
    by_type: Record<string, number>
    by_config: { config_key: string; label: string; contaminated: number }[]
  }
  // SPA-77: confound-controlled effort/efficiency. Primary metric is TOKENS
  // (deterministic), $ secondary (sparse: cost_available=false when un-metered),
  // difficulty-normalized per case (rel_effort). Wall-clock is demoted/caveated.
  effort?: {
    available: boolean
    cost_available: boolean
    // SPA-114: false = no run reported a reasoning split, which is what a
    // non-reasoning model AND a silent provider both look like.
    reasoning_available?: boolean
    primary: string
    per_config: ExperimentEffortRow[]
  } | null
  heatmap: {
    dimensions: string[]
    dimension_labels: Record<string, string>
    rows: {
      config_key: string
      label: string
      cells: Record<string, { mean?: number | null; std?: number | null; n: number }>
      weighted_score: { mean?: number | null; n: number }
    }[]
  }
  // E-02 quality-gate pass-rate per config (SPA-74): did the result clear its
  // CRITICAL rubric thresholds. Over all outcome-scored runs (success or failed).
  // Hidden on verifiable benches (E-02 is the audited subject there).
  quality_gate?: {
    available: boolean
    per_config: {
      config_key: string
      label: string
      n: number
      n_pass: number
      pass_rate?: number | null
      failed_dimensions: Record<string, number>
      // Runs that failed the gate because a critical dimension could not be
      // judged at all — the provider refused the judge's forced tool call. The
      // verdict stands; the cause is not the deliverable (SPA-111).
      n_uncertifiable?: number
    }[]
    n_uncertifiable?: number
  } | null
  trajectory_heatmap: {
    axes: string[]
    axis_labels: Record<string, string>
    rows: {
      config_key: string
      label: string
      cells: Record<string, { mean?: number | null; std?: number | null; n: number }>
      overall_score: { mean?: number | null; n: number }
    }[]
  }
  // E-07 loop-detection rate per config (SPA-74): share of trajectory-scored runs
  // (success OR failed) the process judge flagged as looping — the most actionable
  // process pathology. Counted across failures too (looping often causes them).
  // …plus a deterministic loop anchor (SPA-75): structural_loop_rate COUNTS
  // repeated tool-calls over the full untrimmed trace (LLM-free, a precision-
  // oriented lower bound) next to the judge rate. The two see different inputs/
  // scopes, so we surface the DIRECTIONAL split (judge-only vs counter-only) +
  // Cohen's κ, not just a symmetric agreement %.
  loop_detection?: {
    available: boolean
    structural_available?: boolean
    agreement?: number | null
    kappa?: number | null
    n_judge_only?: number
    n_counter_only?: number
    n_structural?: number
    per_config: {
      config_key: string
      label: string
      n_scored: number
      n_loop: number
      loop_rate?: number | null
      n_structural?: number
      n_structural_loop?: number
      structural_loop_rate?: number | null
      n_judge_only?: number
      n_counter_only?: number
      agreement?: number | null
      kappa?: number | null
    }[]
  } | null
  // SPA-76 reliability gate: per-axis trustworthiness of the E-07 process judge,
  // from REAL calibration only — judge↔human Cohen's κ (E-17) where a human rated
  // the axis, else the judge↔counter loop anchor (SPA-75) for the loop axis, else
  // an honest 'not_calibrated'. Below-threshold axes are quarantined in the UI so
  // an unreliable axis can't silently imply a process "win".
  axis_reliability?: {
    available: boolean
    reliable_kappa: number
    directional_kappa: number
    rank_rho?: number
    min_samples: number
    axes: Record<
      string,
      {
        key: string
        name: string
        source: 'human' | 'structural' | 'none'
        kappa?: number | null
        /** How firmly the point estimate stands where the gate reads it. */
        kappa_ci?: KappaCI | null
        rho?: number | null
        n: number
        status: AxisTrustStatus
      }
    >
  } | null
  // v13: the same reliability traffic light applied to the OUTCOME rubric axes
  // (human source only — the checker verifies verdicts, not per-dimension scores).
  outcome_axis_reliability?: {
    available: boolean
    reliable_kappa: number
    directional_kappa: number
    rank_rho?: number
    min_samples: number
    axes: Record<
      string,
      {
        key: string
        name: string
        source: 'human' | 'structural' | 'none'
        kappa?: number | null
        /** How firmly the point estimate stands where the gate reads it. */
        kappa_ci?: KappaCI | null
        rho?: number | null
        n: number
        status: AxisTrustStatus
      }
    >
  } | null
  // E-06 cleaned-trace stats per config (SPA-74): mean steps + trace compression.
  trace_stats?: {
    available: boolean
    per_config: {
      config_key: string
      label: string
      n: number
      steps_mean?: number | null
      cleaned_tokens_mean?: number | null
      original_tokens_mean?: number | null
      compression?: number | null
    }[]
  } | null
  // E-22 longitudinal: quality/cost across the repetition index (SPA-74).
  longitudinal?: {
    available: boolean
    points: {
      run_index: number
      n: number
      quality_mean?: number | null
      trajectory_mean?: number | null
      cost_mean?: number | null
      tokens_mean?: number | null
    }[]
  } | null
  // E-05 human feedback aggregated per config (SPA-73): the third oracle, shown
  // alongside the judge heatmaps. Over ALL rated runs (not success-only) so the
  // verdict distribution keeps the rejected runs it is about.
  human_feedback?: {
    available: boolean
    dimensions: string[]
    dimension_labels: Record<string, string>
    rows: {
      config_key: string
      label: string
      cells: Record<string, { mean?: number | null; std?: number | null; n: number }>
      overall_score: { mean?: number | null; std?: number | null; n: number }
      n_rated: number
      verdicts: { approve: number; reject: number; none: number }
    }[]
  } | null
  // Where the eval money went per config (SPA-73): agent execution vs each judge.
  cost_breakdown?: {
    available: boolean
    // False = no run here had its orchestrator turns metered, so the total is a
    // lower bound rather than a complete figure.
    orchestrator_metered?: boolean
    per_config: ExperimentCostRow[]
    totals: ExperimentCostRow
  } | null
  trajectory_match: {
    available: boolean
    per_config: {
      config_key: string
      label: string
      n_scored: number
      match_rate?: number | null
      score_mean?: number | null
    }[]
  }
  external?: {
    available: boolean
    per_config: {
      config_key: string
      label: string
      n_evaluated: number
      n_pass: number
      pass_rate?: number | null
      pass_rate_ci?: ProportionCI | null
    }[]
  }
  /** SPA-87: the RQ2 headline. Threshold-free, so it cannot be improved by
   *  choosing a different cut-off after the results are in. */
  judge_discrimination?: {
    available: boolean
    primary: boolean
    overall: ExperimentJudgeDiscriminationCell
    per_config: (ExperimentJudgeDiscriminationCell & { config_key: string; label: string })[]
  }
  rq2?: {
    available: boolean
    /** false since SPA-87 — the 2×2 illustrates the quadrant, it is not the claim. */
    primary?: boolean
    judge_threshold: number
    /** 'pre_registered' = fixed in this experiment's frozen eval_config before it
     *  ran; 'default' = nobody committed to one, so the project fallback applies. */
    threshold_source?: 'pre_registered' | 'default'
    overall: ExperimentRq2Cell
    per_config: (ExperimentRq2Cell & { config_key: string; label: string })[]
    sensitivity?: (ExperimentRq2Cell & { threshold: number; pre_registered: boolean })[]
  }
  pareto: {
    points: {
      config_key: string
      label: string
      quality?: number | null
      cost?: number | null
      effort?: number | null  // SPA-77: token effort (bubble + frontier 3rd dim)
      time?: number | null     // wall-clock — caveated reference only
      on_frontier: boolean
    }[]
    frontier: string[]
  }
  scatter: {
    config_key: string
    label: string
    case_key: string
    run_index: number
    status?: string
    outcome?: number | null
    trajectory?: number | null
    cost: number
    duration?: number | null
    tokens?: number | null
    task_id?: string | null
  }[]
  leaderboard: {
    source: string
    method: string
    status: string
    players: {
      player: string
      label?: string
      rating: number
      ci_low?: number
      ci_high?: number
      rank: number
      wins?: number
      losses?: number
      ties?: number
      win_rate?: number
    }[]
    derivation?: Record<string, unknown>
  }
  significance: ExperimentSignificanceRow[]
  significance_correction: ExperimentSignificanceCorrection
  estimand: ExperimentEstimand
  // SPA-88: the parallel view computed only from the axes that cleared the gate.
  // Raw above keeps every axis — quarantining one is a claim about the JUDGE, and
  // the reader is owed the unfiltered numbers to check it against.
  trusted?: ExperimentTrustedView | null
  failure_modes: {
    per_config: {
      config_key: string
      label: string
      statuses: Record<string, number>
      classes: Record<string, number>
      // Up to 3 representative E-14 reasons per class (highest-confidence first).
      class_reasons?: Record<string, { reason: string; confidence?: number | null }[]>
    }[]
  }
  orchestrator: {
    on?: OrchestratorSide | null
    off?: OrchestratorSide | null
    delta?: Record<string, number | null> | null
  }
  judge_calibration?: {
    available?: boolean
    sample_size: number
    n_records: number
    n_humans: number
    n_dimensions: number
    threshold_kappa: number
    dimensions: {
      key: string
      name: string
      n: number
      pearson?: number | null
      spearman?: number | null
      cohen_kappa?: number | null
      cohen_kappa_ci?: KappaCI | null
      mean_bias?: number | null
      judge_mean?: number | null
      human_mean?: number | null
      reliable: boolean
      status: 'ok' | 'insufficient_data'
    }[]
    overall: {
      n: number
      cohen_kappa?: number | null
      cohen_kappa_ci?: KappaCI | null
      agreement_pct?: number | null
      reliable: boolean
    }
  } | null
  checker_human?: {
    available: boolean
    n: number
    kappa?: number | null
    agreement?: number | null
    cells: {
      pass_approve: number
      pass_reject: number
      fail_approve: number
      fail_reject: number
    }
  }
}
