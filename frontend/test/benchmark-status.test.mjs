import assert from 'node:assert/strict'
import test from 'node:test'
import { availabilityLabels, benchmarkModelRoles } from '../src/lib/benchmarkStatus.ts'

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
