import { NavLink } from 'react-router-dom'
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
} from 'lucide-react'
import { cn } from '@/lib/utils'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/tasks', icon: KanbanSquare, label: 'Task Board' },
  { to: '/chat', icon: MessageSquare, label: 'Chat' },
  { to: '/activity', icon: Activity, label: 'Activity Log' },
  { to: '/analytics', icon: BarChart3, label: 'Analytics' },
  { to: '/experiments', icon: FlaskConical, label: 'Experiments' },
  { to: '/graph', icon: Network, label: 'Graph' },
  { to: '/templates', icon: Boxes, label: 'Templates' },
  { to: '/rubrics', icon: Gauge, label: 'Quality Rubrics' },
  { to: '/knowledge', icon: BookOpen, label: 'Knowledge Base' },
  { to: '/memory', icon: Brain, label: 'Memory' },
  { to: '/settings', icon: Settings2, label: 'Settings' },
]

export default function Sidebar() {
  return (
    <aside className="w-64 bg-gray-900 text-gray-300 flex flex-col">
      <div className="p-4 border-b border-gray-700">
        <h1 className="text-xl font-bold text-white">SpawnHive</h1>
        <p className="text-xs text-gray-500 mt-1">Multi-Agent Orchestrator</p>
      </div>
      <nav className="flex-1 p-3 space-y-1">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
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
        ))}
      </nav>
    </aside>
  )
}
