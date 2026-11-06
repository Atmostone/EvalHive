import { useEffect, useState } from 'react'
import type { JsonSchema } from '@/types'
import { isSecretSetting } from '@/lib/harnessSchema'

const inputClass = 'w-full border rounded px-2 py-1.5 text-sm'

function JsonField({ value, onChange, onValidity }: {
  value: unknown; onChange: (value: unknown) => void; onValidity: (valid: boolean) => void
}) {
  const [draft, setDraft] = useState(JSON.stringify(value, null, 2) ?? '')
  useEffect(() => { setDraft(JSON.stringify(value, null, 2) ?? '') }, [value])
  return <textarea aria-label="JSON value" className={`${inputClass} font-mono min-h-24`}
    value={draft} onChange={event => {
      const text = event.target.value
      setDraft(text)
      try {
        const parsed: unknown = text.trim() ? JSON.parse(text) : undefined
        onValidity(true)
        onChange(parsed)
      } catch { onValidity(false) }
    }} />
}

export function HarnessSettingsEditor({ schema, value, onChange, onValidity }: {
  schema: JsonSchema; value: Record<string, unknown>
  onChange: (value: Record<string, unknown>) => void; onValidity: (valid: boolean) => void
}) {
  const [invalid, setInvalid] = useState<Record<string, boolean>>({})
  const changeObject = (next: unknown) => {
    if (!next || typeof next !== 'object' || Array.isArray(next)) throw new Error('Settings must be an object')
    onChange(next as Record<string, unknown>)
  }
  const validity = (path: string, valid: boolean) => {
    setInvalid(previous => {
      const next = { ...previous, [path]: !valid }
      return next
    })
  }
  useEffect(() => { onValidity(!Object.values(invalid).some(Boolean)) }, [invalid, onValidity])
  const set = (name: string, next: unknown) => {
    const config = { ...value }
    if (next === undefined) delete config[name]
    else config[name] = next
    onChange(config)
  }

  if (!schema.properties) return <JsonField value={value}
    onChange={changeObject} onValidity={valid => validity('$', valid)} />

  return <div className="space-y-3">
    {Object.entries(schema.properties).map(([name, field]) => {
      const current = value[name]
      const label = field.title ?? name
      const defaultHint = field.default === undefined ? 'Use connector default' : `Default: ${JSON.stringify(field.default)}`
      if (isSecretSetting(field)) return <p key={name} className="text-xs text-gray-500">
        {label}: configure a separate credential reference below.
      </p>
      let control
      if (field.enum || field.type === 'boolean') {
        const choices = field.enum ?? [true, false]
        control = <select aria-label={label} className={inputClass}
          value={current === undefined ? '' : JSON.stringify(current)}
          onChange={event => set(name, event.target.value === '' ? undefined : JSON.parse(event.target.value))}>
          <option value="">{defaultHint}</option>
          {choices.map((choice, index) => <option key={index} value={JSON.stringify(choice)}>{String(choice)}</option>)}
        </select>
      } else if (field.type === 'number' || field.type === 'integer') {
        control = <input aria-label={label} type="number" step={field.type === 'integer' ? 1 : 'any'}
          className={inputClass} min={field.minimum} max={field.maximum}
          placeholder={defaultHint} value={current === undefined ? '' : String(current)}
          onChange={event => set(name, event.target.value === '' ? undefined : Number(event.target.value))} />
      } else if (field.type === 'string') {
        control = <input aria-label={label} className={inputClass} placeholder={defaultHint}
          value={current === undefined ? '' : String(current)}
          onChange={event => set(name, event.target.value)} />
      } else {
        control = <JsonField value={current} onChange={next => set(name, next)} onValidity={valid => validity(name, valid)} />
      }
      return <div key={name}>
        <label className="block text-sm font-medium mb-1">{label}{schema.required?.includes(name) ? ' *' : ''}</label>
        {control}
        {current !== undefined && <button type="button" className="text-xs text-blue-600 mt-1"
          onClick={() => { set(name, undefined); validity(name, true) }}>Use default</button>}
        {field.description && <p className="text-xs text-gray-500 mt-1">{field.description}</p>}
      </div>
    })}
    <details className="text-sm">
      <summary className="cursor-pointer text-gray-500">All native settings as JSON</summary>
      <JsonField value={value} onChange={changeObject}
        onValidity={valid => validity('$', valid)} />
    </details>
    {Object.values(invalid).some(Boolean) && <p role="alert" className="text-sm text-red-600">Enter valid JSON for structured settings.</p>}
  </div>
}
