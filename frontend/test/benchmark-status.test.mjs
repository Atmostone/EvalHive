import assert from 'node:assert/strict'
import test from 'node:test'
import { availabilityLabels, benchmarkModelRoles, preflightReason } from '../src/lib/benchmarkStatus.ts'

test('configured metadata does not imply compatibility or live verification', () => {
  assert.deepEqual(availabilityLabels({ installed: true, configured: true, compatible: null, live_verified: false }), [
    'Installed: yes', 'Configured: yes', 'Compatible: select a case and harness', 'Live verified: not verified',
  ])
  const labels = availabilityLabels({ installed: false, configured: null, compatible: false, live_verified: false })
  assert.ok(labels.includes('Installed: no'))
  assert.ok(labels.includes('Configured: not checked'))
  assert.ok(labels.includes('Compatible: no'))
})

test('model roles come from connector requirements and existing optional bindings', () => {
  const manifest = { requirements: { capabilities: ['host.agent_api_v1', 'model.user', 'model.judge', 'model.user'] } }
  assert.deepEqual(benchmarkModelRoles(manifest, { evaluator: 'workspace-model', user: 'other-model' }), ['evaluator', 'judge', 'user'])
  assert.deepEqual(benchmarkModelRoles(undefined, { user: 'model' }), ['user'])
})

test('unknown resources remain unknown in compatibility explanations', () => {
  assert.equal(preflightReason({ code: 'insufficient_resources', resource: 'disk_mb', required: 1000, available: null }),
    'Insufficient disk space (MiB): requires 1000; available unknown.')
  assert.match(preflightReason({ code: 'missing_capability', capability: 'harness.session' }), /harness.session/)
  assert.match(preflightReason({ code: 'future_reason' }), /cannot run/)
})
