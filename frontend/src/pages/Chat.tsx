import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Send, Bot, User, Loader2, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { buildWsUrl, chatApi } from '../api/client'

/** Parse content into segments: regular text, <think> blocks, and tool results */
type Segment = { type: 'text' | 'thinking' | 'tool'; content: string; tool?: string }

function parseMessage(content: string): Segment[] {
  const segments: Segment[] = []

  // First extract tool results
  let cleaned = content
  const toolResults: { tool: string; result: string }[] = []
  const toolRegex = /%%TOOL:(\w+)%%([\s\S]*?)%%ENDTOOL%%/g
  let toolMatch
  while ((toolMatch = toolRegex.exec(content)) !== null) {
    toolResults.push({ tool: toolMatch[1], result: toolMatch[2].trim() })
  }
  cleaned = cleaned.replace(toolRegex, '').trim()

  // Parse think tags from cleaned content
  const thinkRegex = /<think>([\s\S]*?)(?:<\/think>|$)/g
  let lastIndex = 0
  let match

  while ((match = thinkRegex.exec(cleaned)) !== null) {
    if (match.index > lastIndex) {
      const text = cleaned.slice(lastIndex, match.index).trim()
      if (text) segments.push({ type: 'text', content: text })
    }
    const thinking = match[1].trim()
    if (thinking) segments.push({ type: 'thinking', content: thinking })
    lastIndex = thinkRegex.lastIndex
  }

  if (lastIndex < cleaned.length) {
    const text = cleaned.slice(lastIndex).trim()
    if (text) segments.push({ type: 'text', content: text })
  }

  if (segments.length === 0 && cleaned.trim()) {
    segments.push({ type: 'text', content: cleaned.trim() })
  }

  // Append tool results as segments
  for (const tr of toolResults) {
    segments.push({ type: 'tool', content: tr.result, tool: tr.tool })
  }

  return segments
}

function ThinkingBlock({ content }: { content: string }) {
  return (
    <details className="group mb-2">
      <summary className="flex items-center gap-1.5 cursor-pointer text-xs text-gray-400 hover:text-gray-600 select-none py-1">
        <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
        <span>Thinking...</span>
      </summary>
      <div className="mt-1 pl-4 border-l-2 border-gray-200 text-xs text-gray-400 whitespace-pre-wrap">
        {content}
      </div>
    </details>
  )
}

function ToolResultBlock({ tool, content }: { tool: string; content: string }) {
  const labels: Record<string, string> = {
    create_task: 'Task created',
    update_memory: 'Memory updated',
    search_knowledge: 'Knowledge search',
  }
  return (
    <div className="flex items-start gap-2 py-1.5 px-3 bg-purple-50 rounded-lg text-sm border border-purple-100">
      <span className="text-purple-600 font-medium text-xs whitespace-nowrap mt-0.5">{labels[tool] || tool}:</span>
      <span className="text-gray-700">{content}</span>
    </div>
  )
}

function MessageContent({ content }: { content: string }) {
  const segments = parseMessage(content)
  const onlyThinking = segments.every(s => s.type === 'thinking')

  // If message is only thinking with no text or tool result, show a minimal indicator
  if (onlyThinking) {
    return (
      <>
        {segments.map((seg, i) => <ThinkingBlock key={i} content={seg.content} />)}
        <span className="text-xs text-gray-400 italic">Action performed</span>
      </>
    )
  }

  return (
    <>
      {segments.map((seg, i) => {
        if (seg.type === 'thinking') return <ThinkingBlock key={i} content={seg.content} />
        if (seg.type === 'tool') return <ToolResultBlock key={i} tool={seg.tool || ''} content={seg.content} />
        return (
          <div key={i} className="prose prose-sm max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{seg.content}</ReactMarkdown>
          </div>
        )
      })}
    </>
  )
}

interface Message {
  id?: number
  role: 'user' | 'assistant'
  content: string
  created_at?: string
}

export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const wsRef = useRef<WebSocket | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Load history
  const { data: history } = useQuery({
    queryKey: ['chat-history'],
    queryFn: () => chatApi.history(50) as Promise<Message[]>,
  })

  useEffect(() => {
    if (history && messages.length === 0) {
      setMessages(history)
    }
  }, [history, messages.length])

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])

  // WebSocket connection
  const connectWs = useCallback(() => {
    const ws = new WebSocket(buildWsUrl('/ws/chat'))
    wsRef.current = ws

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)

      if (data.type === 'stream') {
        setStreamingContent(prev => prev + data.content)
      } else if (data.type === 'tool_result') {
        setStreamingContent(prev => prev + (prev ? '\n\n' : '') + `%%TOOL:${data.tool}%%${data.result}%%ENDTOOL%%`)
      } else if (data.type === 'done') {
        setStreamingContent(prev => {
          if (prev) {
            setMessages(msgs => [...msgs, { role: 'assistant', content: prev }])
          }
          return ''
        })
        setIsStreaming(false)
      } else if (data.type === 'error') {
        setStreamingContent('')
        setMessages(msgs => [...msgs, { role: 'assistant', content: `Error: ${data.content}` }])
        setIsStreaming(false)
      }
    }

    ws.onclose = () => {
      setTimeout(connectWs, 2000)
    }

    return ws
  }, [])

  useEffect(() => {
    const ws = connectWs()
    return () => { ws.close() }
  }, [connectWs])

  function sendMessage() {
    const content = input.trim()
    if (!content || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return

    setMessages(prev => [...prev, { role: 'user', content }])
    setInput('')
    setIsStreaming(true)
    setStreamingContent('')

    wsRef.current.send(JSON.stringify({ content }))
    inputRef.current?.focus()
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && !isStreaming && (
          <div className="flex flex-col items-center justify-center h-full text-gray-400">
            <Bot className="h-16 w-16 mb-4 text-gray-300" />
            <p className="text-lg font-medium">EvalHive Orchestrator</p>
            <p className="text-sm mt-1">Ask me to create tasks, check status, or explain results</p>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={msg.id || i} className={cn('flex gap-3 max-w-3xl', msg.role === 'user' ? 'ml-auto' : '')}>
            {msg.role === 'assistant' && (
              <div className="w-8 h-8 rounded-full bg-purple-100 flex items-center justify-center flex-shrink-0">
                <Bot className="h-4 w-4 text-purple-600" />
              </div>
            )}
            <div className={cn(
              'rounded-xl px-4 py-3 text-sm',
              msg.role === 'user'
                ? 'bg-blue-600 text-white max-w-md'
                : 'bg-white border max-w-2xl',
            )}>
              {msg.role === 'assistant' ? (
                <MessageContent content={msg.content} />
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
            </div>
            {msg.role === 'user' && (
              <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center flex-shrink-0">
                <User className="h-4 w-4 text-blue-600" />
              </div>
            )}
          </div>
        ))}

        {/* Streaming message */}
        {isStreaming && (
          <div className="flex gap-3 max-w-3xl">
            <div className="w-8 h-8 rounded-full bg-purple-100 flex items-center justify-center flex-shrink-0">
              <Bot className="h-4 w-4 text-purple-600" />
            </div>
            <div className="bg-white border rounded-xl px-4 py-3 text-sm max-w-2xl">
              {streamingContent ? (
                <MessageContent content={streamingContent} />
              ) : (
                <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
              )}
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="border-t bg-white p-4">
        <div className="max-w-3xl mx-auto flex gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Message the orchestrator..."
            rows={1}
            className="flex-1 px-4 py-2.5 border rounded-xl text-sm resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
            disabled={isStreaming}
          />
          <button
            onClick={sendMessage}
            disabled={!input.trim() || isStreaming}
            className="px-4 py-2.5 bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  )
}
