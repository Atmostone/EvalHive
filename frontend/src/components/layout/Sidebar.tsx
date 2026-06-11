import { useEffect } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  KanbanSquare,
  MessageSquare,
  Activity,
  BarChart3,
  FlaskConical,
  Network,
  Boxes,
  Gauge,
  BookOpen,
  Brain,
  Settings2,
  type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useUiMode, NAV_BY_MODE, MODE_HOME, modeForPath, type UiMode } from '@/stores/uiMode'

const ITEMS: Record<string, { icon: LucideIcon; label: string }> = {
  '/': { icon: LayoutDashboard, label: 'Dashboard' },
  '/tasks': { icon: KanbanSquare, label: 'Task Board' },
  '/chat': { icon: MessageSquare, label: 'Chat' },
  '/activity': { icon: Activity, label: 'Activity Log' },
  '/analytics': { icon: BarChart3, label: 'Analytics' },
  '/experiments': { icon: FlaskConical, label: 'Experiments' },
  '/graph': { icon: Network, label: 'Graph' },
  '/templates': { icon: Boxes, label: 'Templates' },
  '/rubrics': { icon: Gauge, label: 'Quality Rubrics' },
  '/knowledge': { icon: BookOpen, label: 'Knowledge Base' },
  '/memory': { icon: Brain, label: 'Memory' },
  '/settings': { icon: Settings2, label: 'Settings' },
}

const MODES: { value: UiMode; icon: LucideIcon; label: string }[] = [
  { value: 'experiments', icon: FlaskConical, label: 'Experiments' },
  { value: 'work', icon: KanbanSquare, label: 'Work' },
]

const SUBTITLE: Record<UiMode, string> = {
  experiments: 'Evaluation Lab',
  work: 'Multi-Agent Orchestrator',
}

export default function Sidebar() {
  const { mode, setMode } = useUiMode()
  const location = useLocation()
  const navigate = useNavigate()

  // Deep link into a page owned by the other mode → follow it, so the active
  // nav item is always visible.
  useEffect(() => {
    const owning = modeForPath(location.pathname)
    if (owning && owning !== mode) setMode(owning)
  }, [location.pathname, mode, setMode])

  const switchMode = (next: UiMode) => {
    if (next === mode) return
    setMode(next)
    // Leaving a page the new mode doesn't show would flip the mode right back.
    if (!NAV_BY_MODE[next].includes(location.pathname)) navigate(MODE_HOME[next])
  }

  return (
    <aside className="w-64 bg-gray-900 text-gray-300 flex flex-col">
      <div className="p-4 border-b border-gray-700">
        <Link to="/">
          <h1 className="text-xl font-bold text-white">SpawnHive</h1>
        </Link>
        <p className="text-xs text-gray-500 mt-1">{SUBTITLE[mode]}</p>
      </div>
      <nav className="flex-1 p-3 space-y-1">
        <div className="flex rounded-lg bg-gray-800 p-1 mb-3">
          {MODES.map((m) => (
            <button
              key={m.value}
              onClick={() => switchMode(m.value)}
              className={cn(
                'flex-1 flex items-center justify-center gap-2 px-2 py-1.5 rounded-md text-sm transition-colors',
                mode === m.value
                  ? 'bg-gray-700 text-white'
                  : 'text-gray-400 hover:text-white'
              )}
            >
              <m.icon className="h-4 w-4" />
              {m.label}
            </button>
          ))}
        </div>
        {NAV_BY_MODE[mode].map((to) => {
          const item = ITEMS[to]
          return (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors',
                  isActive
                    ? 'bg-gray-800 text-white'
                    : 'hover:bg-gray-800 hover:text-white'
                )
              }
            >
              <item.icon className="h-5 w-5" />
              {item.label}
            </NavLink>
          )
        })}
      </nav>
    </aside>
  )
}
