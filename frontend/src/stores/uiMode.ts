import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type UiMode = 'experiments' | 'work'

interface UiModeState {
  mode: UiMode
  setMode: (mode: UiMode) => void
}

export const useUiMode = create<UiModeState>()(
  persist(
    (set) => ({
      mode: 'experiments',
      setMode: (mode) => set({ mode }),
    }),
    { name: 'spawnhive.ui-mode' },
  ),
)

// Ordered nav paths per mode; shared pages (/templates, /activity, /settings) appear in both.
export const NAV_BY_MODE: Record<UiMode, string[]> = {
  experiments: ['/experiments', '/analytics', '/rubrics', '/templates', '/activity', '/settings'],
  work: ['/', '/tasks', '/chat', '/graph', '/knowledge', '/memory', '/templates', '/activity', '/settings'],
}

export const MODE_HOME: Record<UiMode, string> = {
  experiments: '/experiments',
  work: '/',
}

// Mode that exclusively owns a path, or null for shared pages and the root
// (the root is handled by RootRedirect — auto-switching on it would fight the redirect).
export function modeForPath(pathname: string): UiMode | null {
  if (pathname === '/') return null
  if (pathname.startsWith('/experiments')) return 'experiments'
  const exclusive = (mode: UiMode) =>
    NAV_BY_MODE[mode].includes(pathname) && !NAV_BY_MODE[mode === 'work' ? 'experiments' : 'work'].includes(pathname)
  if (exclusive('experiments')) return 'experiments'
  if (exclusive('work')) return 'work'
  return null
}
