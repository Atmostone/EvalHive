import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface AuthUser {
  id: string
  email: string
  display_name?: string | null
}

export interface AuthWorkspace {
  id: string
  name: string
  slug: string
  role: string
}

interface AuthState {
  token: string | null
  user: AuthUser | null
  workspaces: AuthWorkspace[]
  workspaceId: string | null
  setSession: (data: {
    token: string
    user: AuthUser
    workspaceId: string
    workspaces?: AuthWorkspace[]
  }) => void
  setWorkspaces: (workspaces: AuthWorkspace[]) => void
  setWorkspaceId: (id: string) => void
  logout: () => void
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      workspaces: [],
      workspaceId: null,
      setSession: ({ token, user, workspaceId, workspaces }) =>
        set({ token, user, workspaceId, workspaces: workspaces ?? [] }),
      setWorkspaces: (workspaces) => set({ workspaces }),
      setWorkspaceId: (id) => set({ workspaceId: id }),
      logout: () => set({ token: null, user: null, workspaces: [], workspaceId: null }),
    }),
    { name: 'evalhive.auth' },
  ),
)

export function authHeaders(): HeadersInit {
  const { token, workspaceId } = useAuth.getState()
  const h: Record<string, string> = {}
  if (token) h.Authorization = `Bearer ${token}`
  if (workspaceId) h['X-Workspace-Id'] = workspaceId
  return h
}

export function currentRole(): string | null {
  const { workspaceId, workspaces } = useAuth.getState()
  return workspaces.find((w) => w.id === workspaceId)?.role ?? null
}

export function isAdmin(): boolean {
  const r = currentRole()
  return r === 'owner' || r === 'admin'
}
