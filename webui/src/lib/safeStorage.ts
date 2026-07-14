/** localStorage that never throws.
 *
 * `localStorage` is absent or non-functional in some environments — the jsdom
 * test runner, SSR, private-mode Safari with a full quota. Reading it at render
 * init (theme, rail width) crashed the whole component tree there. These wrappers
 * degrade to a no-op / null instead of throwing.
 */
export function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // no-op: storage unavailable or over quota
  }
}
