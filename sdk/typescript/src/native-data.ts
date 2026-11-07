import type {JsonValue} from './protocol.js';

/** Preserve native numeric bounds without relaxing the transport's safe-number rule. */
export function nativePayload(value: unknown): JsonValue {
  const parsed = JSON.parse(JSON.stringify(value)) as JsonValue;
  const paths: string[] = [];
  const convert = (item: JsonValue, path: string): JsonValue => {
    if (typeof item === 'number' && Math.abs(item) > Number.MAX_SAFE_INTEGER) {
      paths.push(path);
      return String(item);
    }
    if (Array.isArray(item)) return item.map((child, index) => convert(child, `${path}/${index}`));
    if (item !== null && typeof item === 'object') return Object.fromEntries(
      Object.entries(item).map(([key, child]) => [key,
        convert(child, `${path}/${key.replaceAll('~', '~0').replaceAll('/', '~1')}`)]));
    return item;
  };
  const converted = convert(parsed, '');
  // Safe payloads keep their historical shape. Strings remain visible to redactors.
  return paths.length ? {encoding: 'evalhive.native-numbers.v1', value: converted, number_paths: paths} : converted;
}
