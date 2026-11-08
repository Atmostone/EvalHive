import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Clock, GitBranch } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import type { Task } from '@/types'
import { PRIORITY_COLORS } from '@/types'
import { cn } from '@/lib/utils'

interface TaskCardProps {
  task: Task
  onClick: (task: Task) => void
}

export default function TaskCard({ task, onClick }: TaskCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: task.id, data: { task }, disabled: !!task.benchmark_trial_id })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      aria-disabled={false}
      onKeyDown={event => {
        if (task.benchmark_trial_id) {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick(task) }
        } else listeners?.onKeyDown?.(event)
      }}
      onClick={() => onClick(task)}
      className={cn(
        'bg-white rounded-lg border p-3 cursor-pointer hover:shadow-md transition-shadow',
        isDragging && 'opacity-50 shadow-lg',
      )}
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <h4 className="text-sm font-medium text-gray-900 line-clamp-2">{task.title}</h4>
        <div className="flex items-center gap-1 shrink-0">
          {task.benchmark_trial_id && task.replay_of_task_id && <span className="text-xs px-1.5 py-0.5 rounded-full bg-blue-100 text-blue-700">repeat</span>}
          {task.origin === 'experiment' && (
            <span className="text-xs px-1.5 py-0.5 rounded-full whitespace-nowrap bg-purple-100 text-purple-700" title="Experiment cell run">
              exp
            </span>
          )}
          <span className={cn('text-xs px-1.5 py-0.5 rounded-full whitespace-nowrap', PRIORITY_COLORS[task.priority])}>
            {task.priority}
          </span>
        </div>
      </div>

      {task.description && (
        <p className="text-xs text-gray-500 line-clamp-2 mb-2">{task.description}</p>
      )}

      <div className="flex items-center gap-2 text-xs text-gray-400">
        <Clock className="h-3 w-3" />
        <span>{formatDistanceToNow(new Date(task.created_at))} ago</span>
        {task.subtasks && task.subtasks.length > 0 && (
          <>
            <GitBranch className="h-3 w-3 ml-1" />
            <span>{task.subtasks.length}</span>
          </>
        )}
      </div>
    </div>
  )
}
