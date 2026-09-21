import { memo } from 'react'
import { BaseEdge, EdgeLabelRenderer, getBezierPath } from 'reactflow'
import type { EdgeProps } from 'reactflow'

export interface EventEdgeData {
  count: number
  color: string
  pulse: boolean
}

/**
 * Edge color by event_type — exported for reuse in legend.
 */
export const EDGE_COLORS = {
  blue: '#3b82f6',
  green: '#10b981',
  orange: '#f97316',
  red: '#ef4444',
  gray: '#9ca3af',
} as const

export type EdgeColorKey = keyof typeof EDGE_COLORS

export function colorForEventType(eventType: string): EdgeColorKey {
  switch (eventType) {
    case 'agent_message':
    case 'task_status_changed':
      return 'blue'
    case 'agent_completed':
    case 'agent_progress':
      return 'green'
    case 'orchestrator_feedback':
    case 'orchestrator_decision':
      return 'orange'
    case 'agent_failed':
    case 'agent_killed':
    case 'agent_aborted':
      return 'red'
    default:
      return 'gray'
  }
}

function EventEdgeAnimComponent(props: EdgeProps<EventEdgeData>) {
  const {
    id,
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    markerEnd,
    data,
  } = props

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  })

  const color = data?.color ?? EDGE_COLORS.gray
  const pulse = data?.pulse ?? false
  const count = data?.count ?? 0

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          stroke: color,
          strokeWidth: pulse ? 3 : 1.5,
          transition: 'stroke-width 600ms ease-out, stroke 600ms ease-out',
          opacity: pulse ? 1 : 0.75,
        }}
      />
      {pulse && (
        <path
          d={edgePath}
          fill="none"
          stroke={color}
          strokeWidth={6}
          opacity={0.35}
          style={{
            filter: 'blur(2px)',
            animation: 'evalhive-edge-pulse 600ms ease-out',
          }}
        />
      )}
      <EdgeLabelRenderer>
        <div
          style={{
            position: 'absolute',
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            pointerEvents: 'all',
          }}
          className="px-1.5 py-0.5 rounded bg-white border border-gray-200 text-[10px] font-medium text-gray-700 shadow-sm nodrag nopan"
        >
          {count}
        </div>
      </EdgeLabelRenderer>
      <style>{`
        @keyframes evalhive-edge-pulse {
          0% { opacity: 0.6; stroke-width: 8px; }
          100% { opacity: 0; stroke-width: 2px; }
        }
      `}</style>
    </>
  )
}

export const EventEdgeAnim = memo(EventEdgeAnimComponent)
