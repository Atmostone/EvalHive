import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { configurationErrors, isSecretSetting } from '../src/lib/harnessSchema.ts'

for (const name of ['pi', 'opencode']) {
  const manifest = JSON.parse(readFileSync(new URL(`../../harnesses/${name}/manifest.json`, import.meta.url)))
  test(`${name} profile settings validate without applying hidden defaults`, () => {
    const config = {}
    assert.deepEqual(configurationErrors(manifest.config_schema, config), [])
    assert.deepEqual(config, {})
    assert.ok(configurationErrors(manifest.config_schema, { uninstalledPlugin: 'secret-input' }).length)
    assert.ok(configurationErrors(manifest.config_schema, { contextWindow: 0 }).length)
    assert.ok(configurationErrors(manifest.config_schema, null).length)
  })
}

test('an external connector schema supports local references and nested settings', () => {
  const schema = {
    type: 'object', additionalProperties: false,
    $defs: { strategy: { type: 'string', enum: ['safe', 'fast'] } },
    properties: { native: { type: 'object', properties: { strategy: { $ref: '#/$defs/strategy' } }, required: ['strategy'] } },
    required: ['native'],
  }
  assert.deepEqual(configurationErrors(schema, { native: { strategy: 'safe' } }), [])
  assert.ok(configurationErrors(schema, { native: { strategy: 'invalid' } }).length)
  assert.ok(configurationErrors(schema, {}).length)
  for (const setting of [{ writeOnly: true }, { format: 'password' }, { 'x-secret': true }]) {
    assert.equal(isSecretSetting(setting), true)
  }
  assert.equal(isSecretSetting({ type: 'string' }), false)
})
