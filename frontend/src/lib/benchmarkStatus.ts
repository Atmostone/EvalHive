import type { BenchmarkAvailability, BenchmarkManifest } from '../types/benchmarks.ts'

export function availabilityLabels(value: BenchmarkAvailability) {
  return [
    `Installed: ${value.installed ? 'yes' : 'no'}`,
    `Configured: ${value.configured === null ? 'not checked' : value.configured ? 'yes' : 'no'}`,
    `Compatible: ${value.compatible === null ? 'select a case and harness' : value.compatible ? 'yes' : 'no'}`,
    `Live verified: ${value.live_verified ? 'yes' : 'not verified'}`,
  ]
}

export function benchmarkModelRoles(manifest: BenchmarkManifest | undefined, bindings: Record<string, string>) {
  return [...new Set([
    ...(manifest?.requirements.capabilities ?? []).filter(name => name.startsWith('model.')).map(name => name.slice(6)),
    ...Object.keys(bindings),
  ])].sort()
}

export function preflightReason(reason: { code: string; [key: string]: unknown }): string {
  const text = (name: string) => typeof reason[name] === 'string' || typeof reason[name] === 'number' ? String(reason[name]) : 'unknown'
  const resource = ({ memory_mb: 'memory (MiB)', disk_mb: 'disk space (MiB)', cpu_cores: 'CPU cores' } as Record<string, string>)[text('resource')] ?? text('resource')
  switch (reason.code) {
    case 'harness_unavailable': return 'The selected harness version is not installed.'
    case 'runtime_unavailable': return 'Benchmark execution is not enabled on this server.'
    case 'missing_capability': return `This selection does not provide a required capability: ${text('capability')}.`
    case 'platform_mismatch': return `This host does not meet the required ${text('field')}: ${text('required')}.`
    case 'insufficient_resources': return `Insufficient ${resource}: requires ${text('required')}; available ${text('available')}.`
    case 'conflicting_native_requirements': return 'The benchmark and case have conflicting requirements.'
    case 'installed_manifest_changed': return 'The installed connector differs from the version recorded for this dataset. Register the matching revision.'
    default: return 'This selection cannot run with the current configuration.'
  }
}
