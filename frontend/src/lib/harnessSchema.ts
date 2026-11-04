import Ajv2020 from 'ajv/dist/2020.js'
import type { JsonSchema } from '../types/index.ts'

// Schemas originate in the installed, server-validated catalog. Defaults are
// hints only: validation must not change the configuration being saved.
const ajv = new Ajv2020({ allErrors: true, strict: false, validateFormats: false })
const validators = new WeakMap<JsonSchema, ReturnType<typeof ajv.compile>>()

export function configurationErrors(schema: JsonSchema, config: unknown): string[] {
  try {
    let validate = validators.get(schema)
    if (!validate) {
      validate = ajv.compile(schema)
      validators.set(schema, validate)
    }
    if (validate(config)) return []
    return (validate.errors ?? []).map(error =>
      `${error.instancePath || 'Settings'} ${error.message ?? 'is invalid'}`)
  } catch {
    return ['The installed connector settings schema cannot be rendered.']
  }
}

export function isSecretSetting(schema: JsonSchema): boolean {
  return !!(schema.writeOnly || schema.format === 'password' || schema['x-secret'])
}
