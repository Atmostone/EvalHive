import type { JsonSchema } from './index'

export interface BenchmarkAvailability {
  installed: boolean
  configured: boolean | null
  compatible: boolean | null
  live_verified: boolean
  missing?: string[]
}

export interface BenchmarkManifest {
  id: string
  version: string
  display_name: string
  protocol_version: string
  config_schema: JsonSchema
  secret_schema?: JsonSchema
  requirements: { capabilities?: string[]; [key: string]: unknown }
  capabilities: Record<string, 'native' | 'emulated' | 'unsupported'>
}

export interface BenchmarkCatalogEntry extends BenchmarkAvailability {
  manifest: BenchmarkManifest
}

export interface BenchmarkProfileInput {
  name: string
  connector_id: string
  connector_version?: string
  config: Record<string, unknown>
  secret_refs: Record<string, string>
  model_bindings: Record<string, string>
}

export interface BenchmarkProfile extends BenchmarkProfileInput, BenchmarkAvailability {
  id: string
  connector_version: string
  configured: boolean
  created_at: string
  updated_at: string
}

export interface BenchmarkCredential {
  id: string
  name: string
  configured: boolean
}

export interface BenchmarkDataset {
  id: string
  connector_id: string
  connector_version: string
  dataset_id: string
  revision: string
  source_digest: string
  receipt_digest: string
  worker_image: string
  case_count: number
  created_at: string
  live_verified: boolean
}

export interface BenchmarkCase {
  id: string
  ref: { dataset_id: string; revision: string; case_id: string; split?: string }
  title: string
  description?: string
  source_digest: string
  requirements: Record<string, unknown>
}

export interface BenchmarkPreflight extends BenchmarkAvailability {
  stage: string
  can_run: boolean
  runtime_admission?: string
  reasons: { code: string; [key: string]: unknown }[]
  dataset_revision_id?: string
  case_id?: string
  harness_profile_id?: string
}

export interface BenchmarkTrial {
  id: string
  task_id: string | null
  profile_id: string | null
  dataset_case_id: string | null
  snapshot: Record<string, unknown>
  phase: string
  execution_status: string
  evaluation_status: string
  recording_status: string
  cleanup_status: string
  cancel_requested_at: string | null
  created_at: string
  finished_at: string | null
  job_id?: string
  job_status?: string
}

export interface BenchmarkEvaluation {
  id: string
  kind: string
  status: string
  verdict: string | null
  success: boolean | null
  metrics: { name: string; value: unknown; direction?: string; aggregation?: string }[]
  error_kind: string | null
  started_at: string | null
  finished_at: string | null
}

export interface BenchmarkArtifact {
  id: string
  artifact: { id: string; visibility: string; size_bytes: number; digest: string; media_type: string }
  created_at: string
}

export interface BenchmarkJournalEvent {
  seq: number
  event: string
  data: Record<string, unknown>
  created_at: string
}
