import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import Sidebar from './components/layout/Sidebar'
import WorkspaceSwitcher from './components/layout/WorkspaceSwitcher'
import Dashboard from './pages/Dashboard'
import TaskBoard from './pages/TaskBoard'
import Chat from './pages/Chat'
import ActivityLog from './pages/ActivityLog'
import Analytics from './pages/Analytics'
import Experiments from './pages/Experiments'
import ExperimentDetail from './pages/ExperimentDetail'
import Graph from './pages/Graph'
import Templates from './pages/Templates'
import Rubrics from './pages/Rubrics'
import KnowledgeBase from './pages/KnowledgeBase'
import Memory from './pages/Memory'
import Settings from './pages/Settings'
import Login from './pages/Login'
import Register from './pages/Register'
import { useAuth } from './stores/auth'
import { useUiMode } from './stores/uiMode'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const token = useAuth((s) => s.token)
  const location = useLocation()
  if (!token) return <Navigate to="/login" state={{ from: location }} replace />
  return <>{children}</>
}

function RootRedirect() {
  const mode = useUiMode((s) => s.mode)
  if (mode === 'experiments') return <Navigate to="/experiments" replace />
  return <Dashboard />
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route
        path="*"
        element={
          <RequireAuth>
            <div className="flex h-screen bg-gray-50">
              <Sidebar />
              <main className="flex-1 overflow-auto flex flex-col">
                <WorkspaceSwitcher />
                <div className="flex-1 overflow-auto">
                  <Routes>
                    <Route path="/" element={<RootRedirect />} />
                    <Route path="/tasks" element={<TaskBoard />} />
                    <Route path="/chat" element={<Chat />} />
                    <Route path="/activity" element={<ActivityLog />} />
                    <Route path="/analytics" element={<Analytics />} />
                    <Route path="/experiments" element={<Experiments />} />
                    <Route path="/experiments/:id" element={<ExperimentDetail />} />
                    <Route path="/graph" element={<Graph />} />
                    <Route path="/templates" element={<Templates />} />
                    <Route path="/rubrics" element={<Rubrics />} />
                    <Route path="/knowledge" element={<KnowledgeBase />} />
                    <Route path="/memory" element={<Memory />} />
                    <Route path="/settings" element={<Settings />} />
                  </Routes>
                </div>
              </main>
            </div>
          </RequireAuth>
        }
      />
    </Routes>
  )
}
