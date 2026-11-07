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
