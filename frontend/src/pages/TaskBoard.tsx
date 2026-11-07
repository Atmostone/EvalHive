import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { DndContext, DragEndEvent, DragOverlay, PointerSensor, useSensor, useSensors } from '@dnd-kit/core'
import { tasksApi } from '@/api/client'
import type { Task, TaskStatus } from '@/types'
import { KANBAN_COLUMNS } from '@/types'
import { Plus } from 'lucide-react'
import KanbanColumn from '@/components/tasks/KanbanColumn'
import TaskCard from '@/components/tasks/TaskCard'
import TaskDetail from '@/components/tasks/TaskDetail'
import CreateTaskModal from '@/components/tasks/CreateTaskModal'

export default function TaskBoard() {
  const queryClient = useQueryClient()
  const [selectedTask, setSelectedTask] = useState<Task | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [activeTask, setActiveTask] = useState<Task | null>(null)
  const [includeExperiments, setIncludeExperiments] = useState(false)

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } })
  )

  const { data: tasks = [] } = useQuery({
    queryKey: ['tasks', { includeExperiments }],
    queryFn: () => tasksApi.list({ include_experiments: includeExperiments }),
    refetchInterval: 5000,
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: TaskStatus }) =>
      tasksApi.update(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tasks'] }),
  })

  // Only show top-level tasks (no parent_id)
  const topLevelTasks = tasks.filter(t => !t.parent_id)

  const tasksByStatus = KANBAN_COLUMNS.reduce<Record<TaskStatus, Task[]>>((acc, status) => {
    acc[status] = topLevelTasks.filter(t => t.status === status)
    return acc
  }, {} as Record<TaskStatus, Task[]>)

  function handleDragStart(event: { active: { data: { current?: { task?: Task } } } }) {
    setActiveTask(event.active.data.current?.task || null)
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveTask(null)
    const { active, over } = event
    if (!over) return

    const taskId = active.id as string
    const newStatus = over.id as string

    // Validate the drop target is a column
    if (!KANBAN_COLUMNS.includes(newStatus as TaskStatus)) return

    const task = tasks.find(t => t.id === taskId)
    if (!task || task.status === newStatus || task.benchmark_trial_id) return

    // Don't allow dragging in_progress tasks back to backlog
    if (task.status === 'in_progress' && newStatus === 'backlog') return

    updateMutation.mutate({ id: taskId, status: newStatus as TaskStatus })
  }

  return (
    <div className="p-6 h-full flex flex-col">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold text-gray-900">Task Board</h1>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer" title="Experiment-cell runs are hidden by default">
            <input type="checkbox" checked={includeExperiments} onChange={(e) => setIncludeExperiments(e.target.checked)} />
            show experiment tasks
          </label>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium"
          >
            <Plus className="h-4 w-4" /> New Task
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-x-auto">
        <DndContext
          sensors={sensors}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
        >
          <div className="flex gap-3 min-h-[400px] pb-4">
            {KANBAN_COLUMNS.map(status => (
              <KanbanColumn
                key={status}
                status={status}
                tasks={tasksByStatus[status] || []}
                onTaskClick={setSelectedTask}
              />
            ))}
          </div>

          <DragOverlay>
            {activeTask && (
              <div className="w-[240px]">
                <TaskCard task={activeTask} onClick={() => {}} />
              </div>
            )}
          </DragOverlay>
        </DndContext>
      </div>

      {selectedTask && (
        <TaskDetail task={selectedTask} onClose={() => setSelectedTask(null)} />
      )}

      {showCreate && (
        <CreateTaskModal onClose={() => setShowCreate(false)} />
      )}
    </div>
  )
}
