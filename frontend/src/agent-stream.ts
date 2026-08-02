export type ParsedSseEvent = {
  event: string
  data: string
}

export type TraceEvent = {
  event_id?: string
  trace_id: string
  node_name: string
  status: string
  latency_ms: number
  input_ref: string | null
  output_ref: string | null
  error: unknown
  metadata: Record<string, unknown>
}

export type AgentStreamView = {
  thinking: string
  thinkingSections?: AgentThinkingSection[]
  answer: string
  progress: AgentProgress | null
  traceEvents: TraceEvent[]
}

export type AgentThinkingSection = {
  stage: string
  label: string
  content: string
}

export type AgentProgress = {
  stage: string
  message: string
}

export type TextareaKeyLike = {
  key: string
  shiftKey: boolean
  isComposing: boolean
  keyCode: number
}

export function parseSseEvent(rawEvent: string): ParsedSseEvent | null {
  const lines = rawEvent.split('\n')
  let event = 'message'
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim()
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart())
    }
  }
  if (!dataLines.length) {
    return null
  }
  return { event, data: dataLines.join('\n') }
}

export function applyAgentStreamEvent(
  view: AgentStreamView,
  parsed: ParsedSseEvent
): void {
  if (parsed.event === 'thinking_delta') {
    const payload = JSON.parse(parsed.data) as {
      stage?: string
      label?: string
      content?: string
    }
    const content = payload.content ?? ''
    view.thinking += content
    if (view.thinkingSections && content) {
      appendThinkingSectionDelta(view.thinkingSections, {
        stage: payload.stage ?? 'analysis',
        label: payload.label ?? '分析',
        content
      })
    }
    return
  }
  if (parsed.event === 'answer_delta') {
    const payload = JSON.parse(parsed.data) as { content?: string }
    view.progress = null
    view.answer += payload.content ?? ''
    return
  }
  if (parsed.event === 'progress') {
    view.progress = JSON.parse(parsed.data) as AgentProgress
    return
  }
  if (parsed.event === 'agent_event') {
    view.traceEvents.push(JSON.parse(parsed.data) as TraceEvent)
  }
}

export function appendThinkingSectionDelta(
  sections: AgentThinkingSection[],
  delta: AgentThinkingSection
): void {
  const last = sections[sections.length - 1]
  if (last && last.stage === delta.stage) {
    last.content += delta.content
    return
  }
  sections.push({ ...delta })
}

export function shouldSubmitTextareaKey(event: TextareaKeyLike): boolean {
  return (
    event.key === 'Enter' &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  )
}

export function shouldUseFinalAnswerFallback(input: {
  receivedAnswerDelta: boolean
  currentContent: string
  finalContent: string
}): boolean {
  return (
    !input.receivedAnswerDelta &&
    !input.currentContent &&
    Boolean(input.finalContent)
  )
}

export function finalThinkingContent(input: {
  thinking: string
  userMessage: string
}): string {
  const thinking = input.thinking.trim()
  if (thinking) {
    return thinking
  }
  const target = input.userMessage.trim() || '你的问题'
  return `我会先拆解你的问题“${target}”，确认任务目标和需要使用的数据来源，再基于可用证据生成最终回答。`
}
