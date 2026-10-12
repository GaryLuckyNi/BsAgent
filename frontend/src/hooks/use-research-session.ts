import type { Message } from '@langchain/langgraph-sdk'
import { FetchStreamTransport, useStream } from '@langchain/langgraph-sdk/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  resolveConversationSession,
  toConversationMessages,
  type RestoredResearchProcessEvent
} from '../conversation-session'
import {
  buildEvidenceIndex,
  type ResearchMessage,
  type ResearchProgress,
  type ResearchReasoning
} from '../domain/research'
import {
  agentProtocolStreamUrl,
  agentRequestHeaders,
  checkAgentHealth,
  loadSessionMessages,
  type RequestIdentity
} from '../services/research-api'

type ResearchGraphState = {
  messages: Message[]
}

type ResearchGraphUpdate = {
  messages: Array<{ type: 'human'; id: string; content: string }>
  attachment_ids: string[]
  request_id: string
}

type AgentProtocolEvent = {
  type: 'progress' | 'thinking_delta' | 'final' | 'approval_required'
  id?: string
  stage?: string
  label?: string
  message?: string
  status?: 'started' | 'completed' | 'failed'
  kind?: 'status' | 'activity' | 'summary'
  activity_type?: 'web_search' | 'market_data' | 'evidence'
  group_id?: string
  task_id?: string
  task_label?: string
  query?: string
  result_count?: number
  result_urls?: string[]
  content?: string
  response?: {
    content?: string
    cited_evidence_ids?: string[]
  }
}

type ResearchStreamBag = {
  UpdateType: ResearchGraphUpdate
  CustomEventType: AgentProtocolEvent
}

const sampleEvidence = buildEvidenceIndex([
  {
    id: 'sse-market-2026',
    publisher: '上海证券交易所',
    title: '市场数据与交易公开信息',
    url: 'https://www.sse.com.cn/market/',
    publishedAt: '2026-08-28',
    summary: '用于核验指数、成交与市场结构数据。'
  },
  {
    id: 'pbc-policy',
    publisher: '中国人民银行',
    title: '货币政策执行报告',
    url: 'https://www.pbc.gov.cn/zhengcehuobisi/125207/125227/125957/index.html',
    publishedAt: '2026-08-15',
    summary: '观察流动性、利率与信用环境的权威材料。'
  },
  {
    id: 'stats-industry',
    publisher: '国家统计局',
    title: '规模以上工业企业数据',
    url: 'https://data.stats.gov.cn/',
    publishedAt: '2026-08-27',
    summary: '用于跟踪行业景气度和盈利周期。'
  }
])

const welcomeMessage: ResearchMessage = {
  id: 'welcome-analysis',
  role: 'assistant',
  createdAt: new Date().toISOString(),
  status: 'complete',
  citations: [1, 2, 3],
  evidence: sampleEvidence,
  content: `### 研究摘要\n\n当前 A 股的核心变量不是单一指数点位，而是**流动性改善能否传导到企业盈利**。交易数据显示风格正在从纯红利向“稳定现金流 + 盈利修复”扩散。\n\n- **基准情景**：仓位保持中性，优先配置有盈利验证的科技制造与高股息资产。\n- **上行触发**：社融与工业利润连续改善，可提高顺周期暴露。\n- **风险触发**：成交结构快速收缩或盈利预期下修时，应降低组合波动率。`
}

export function useResearchSession(identity: RequestIdentity) {
  const sessionId = useMemo(() => resolveConversationSession(
    window.localStorage,
    identity,
    () => `session_${crypto.randomUUID().slice(0, 8)}`
  ), [identity])
  const [messages, setMessages] = useState<ResearchMessage[]>([welcomeMessage])
  const [isOnline, setIsOnline] = useState<boolean | null>(null)
  const [error, setError] = useState('')
  const activeAssistantIdRef = useRef<string | null>(null)
  const streamMessageIdsAtSubmitRef = useRef<Set<string>>(new Set())
  const lastStreamedAnswerRef = useRef<{ assistantId: string; content: string } | null>(null)
  const eventSequenceRef = useRef(0)
  const messagesRef = useRef(messages)

  useEffect(() => {
    messagesRef.current = messages
  }, [messages])

  const transport = useMemo(
    () => new FetchStreamTransport<ResearchGraphState, ResearchStreamBag>({
      apiUrl: agentProtocolStreamUrl(sessionId),
      defaultHeaders: agentRequestHeaders(identity)
    }),
    [identity, sessionId]
  )

  const handleCustomEvent = useCallback((event: AgentProtocolEvent) => {
    const assistantId = activeAssistantIdRef.current
    if (!assistantId) return
    if (event.type === 'thinking_delta' && event.content) {
      const sequence = eventSequenceRef.current++
      setMessages((current) => updateMessage(current, assistantId, (message) => ({
        ...message,
        reasoning: upsertReasoningDelta(message.reasoning, {
          id: event.id || event.stage || 'reasoning',
          sequence,
          label: event.label || '分析',
          content: event.content || ''
        })
      })))
      return
    }
    if (event.type === 'progress' && event.message) {
      if (!event.kind || event.kind === 'status') {
        setMessages((current) => updateMessage(current, assistantId, (message) => ({
          ...message,
          processStatus: event.message
        })))
        return
      }
      const progress: ResearchProgress = {
        sequence: eventSequenceRef.current++,
        id: event.id || `${event.stage || 'analysis'}:${event.message}`,
        kind: event.kind,
        stage: event.stage,
        label: event.label || '分析',
        message: event.message,
        status: event.status || 'started',
        activityType: event.activity_type,
        groupId: event.group_id,
        queryCount: event.activity_type && event.task_id ? 1 : undefined,
        resultCount: event.activity_type === 'web_search' ? (event.result_count || 0) : undefined,
        queries: event.activity_type && event.task_id ? [{
          taskId: event.task_id,
          query: event.task_label || event.query || event.message,
          status: event.status || 'started',
          resultUrls: uniqueUrls(event.result_urls)
        }] : undefined
      }
      setMessages((current) => updateMessage(current, assistantId, (message) => ({
        ...message,
        progress: progress.activityType && progress.groupId
          ? mergeActivityProgress(message.progress, progress)
          : upsertProgress(message.progress, progress)
      })))
      return
    }
    if (event.type === 'final') {
      const response = event.response || {}
      const evidence = buildEvidenceIndex((response.cited_evidence_ids || []).map((id) => ({
        id,
        publisher: '投资研究数据源',
        title: `引用资料 ${id}`,
        url: '#',
        summary: '原始资料地址将由后端证据元数据补充。'
      })))
      setMessages((current) => updateMessage(current, assistantId, (message) => ({
        ...message,
        content: response.content || message.content,
        status: 'complete',
        evidence,
        citations: evidence.map((item) => item.index)
      })))
    }
  }, [])

  const stream = useStream<ResearchGraphState, ResearchStreamBag>({
    transport,
    threadId: sessionId,
    throttle: false,
    initialValues: { messages: [] },
    onCustomEvent: handleCustomEvent,
    onError: (caught) => {
      const message = errorMessage(caught)
      setError(message)
      const assistantId = activeAssistantIdRef.current
      if (assistantId) {
        setMessages((current) => updateMessage(current, assistantId, (item) => ({
          ...item,
          content: item.content || message,
          status: 'error'
        })))
      }
    },
    onFinish: () => {
      const assistantId = activeAssistantIdRef.current
      if (assistantId) {
        setMessages((current) => updateMessage(current, assistantId, (message) => ({
          ...message,
          status: message.status === 'error' ? 'error' : 'complete'
        })))
      }
      activeAssistantIdRef.current = null
      streamMessageIdsAtSubmitRef.current.clear()
    }
  })

  useEffect(() => {
    void checkAgentHealth().then(setIsOnline)
    void loadSessionMessages(sessionId, identity).then((items) => {
      if (!items.length) return
      const restored = toConversationMessages(items).map<ResearchMessage>((item) => ({
        id: crypto.randomUUID(),
        role: item.role === 'agent' ? 'assistant' : 'user',
        content: item.content,
        createdAt: new Date().toISOString(),
        status: 'complete',
        progress: restoreResearchProgress(item.researchProcess)
      }))
      setMessages(restored)
    }).catch(() => undefined)
  }, [identity, sessionId])

  useEffect(() => {
    const assistantId = activeAssistantIdRef.current
    if (!assistantId) return
    const latestAssistant = [...stream.messages]
      .reverse()
      .find((message) => (
        message.type === 'ai'
        && typeof message.id === 'string'
        && !streamMessageIdsAtSubmitRef.current.has(message.id)
      ))
    const content = messageText(latestAssistant)
    if (!content) return
    const lastStreamedAnswer = lastStreamedAnswerRef.current
    if (
      lastStreamedAnswer?.assistantId === assistantId
      && lastStreamedAnswer.content === content
    ) return
    lastStreamedAnswerRef.current = { assistantId, content }
    setMessages((current) => updateMessage(current, assistantId, (message) => ({
      ...message,
      content
    })))
  }, [stream.messages])

  const sendMessage = useCallback(async (content: string, attachmentIds: string[] = []) => {
    if (!content.trim() || stream.isLoading) return
    setError('')
    const text = content.trim()
    const userMessage: ResearchMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
      createdAt: new Date().toISOString(),
      status: 'complete'
    }
    const assistantId = crypto.randomUUID()
    const optimisticMessages = [
      ...messagesRef.current.map(toGraphMessage),
      toGraphMessage(userMessage)
    ]
    streamMessageIdsAtSubmitRef.current = new Set(
      [...stream.messages, ...optimisticMessages].flatMap((message) => (
        typeof message.id === 'string' ? [message.id] : []
      ))
    )
    eventSequenceRef.current = 0
    activeAssistantIdRef.current = assistantId
    lastStreamedAnswerRef.current = null
    setMessages((current) => [...current, userMessage, {
      id: assistantId,
      role: 'assistant',
      content: '',
      reasoning: [],
      progress: [],
      processStatus: '正在理解问题与研究范围。',
      createdAt: new Date().toISOString(),
      status: 'streaming'
    }])
    await stream.submit(
      {
        messages: [{ type: 'human', id: userMessage.id, content: text }],
        attachment_ids: attachmentIds,
        request_id: `req_${crypto.randomUUID()}`
      },
      {
        optimisticValues: {
          messages: optimisticMessages
        }
      }
    )
  }, [stream])

  const cancelMessage = useCallback(() => {
    void stream.stop()
  }, [stream])

  return {
    sessionId,
    messages,
    isStreaming: stream.isLoading,
    isOnline,
    error,
    sendMessage,
    cancelMessage
  }
}

function updateMessage(messages: ResearchMessage[], id: string, update: (message: ResearchMessage) => ResearchMessage) {
  return messages.map((message) => message.id === id ? update(message) : message)
}

function upsertProgress(
  current: ResearchProgress[] | undefined,
  incoming: ResearchProgress
): ResearchProgress[] {
  const items = [...(current || [])]
  const index = items.findIndex((item) => item.id === incoming.id)
  if (index >= 0) items[index] = { ...incoming, sequence: items[index].sequence }
  else items.push(incoming)
  return items
}

function mergeActivityProgress(
  current: ResearchProgress[] | undefined,
  incoming: ResearchProgress
): ResearchProgress[] {
  const items = [...(current || [])]
  const index = items.findIndex((item) => (
    item.activityType === incoming.activityType && item.groupId === incoming.groupId
  ))
  const existing = index >= 0 ? items[index] : undefined
  const queries = [...(existing?.queries || [])]
  for (const query of incoming.queries || []) {
    const queryIndex = queries.findIndex((item) => item.taskId === query.taskId)
    if (queryIndex >= 0) queries[queryIndex] = query
    else queries.push(query)
  }
  const resultUrls = uniqueUrls(queries.flatMap((query) => query.resultUrls))
  const status = queries.some((query) => query.status === 'started')
    ? 'started'
    : queries.some((query) => query.status === 'completed')
      ? 'completed'
      : 'failed'
  const merged: ResearchProgress = {
    ...incoming,
    id: incoming.groupId || incoming.id,
    sequence: existing?.sequence ?? incoming.sequence,
    status,
    queryCount: queries.length,
    resultCount: resultUrls.length,
    queries
  }
  if (index >= 0) items[index] = merged
  else items.push(merged)
  return items
}

function restoreResearchProgress(
  events: RestoredResearchProcessEvent[] | undefined
): ResearchProgress[] | undefined {
  let restored: ResearchProgress[] = []
  for (const event of events || []) {
    const progress: ResearchProgress = {
      id: event.id,
      sequence: event.sequence,
      kind: event.kind,
      label: event.label,
      message: event.message,
      status: event.status,
      activityType: event.activityType,
      groupId: event.groupId,
      queryCount: event.activityType && event.taskId ? 1 : undefined,
      resultCount: event.activityType === 'web_search' ? (event.resultCount || 0) : undefined,
      queries: event.activityType && event.taskId ? [{
        taskId: event.taskId,
        query: event.taskLabel || event.query || event.message,
        status: event.status,
        resultUrls: uniqueUrls(event.resultUrls)
      }] : undefined
    }
    restored = progress.activityType && progress.groupId
      ? mergeActivityProgress(restored, progress)
      : upsertProgress(restored, progress)
  }
  return restored.length ? restored : undefined
}

function uniqueUrls(urls: string[] | undefined): string[] {
  return [...new Set((urls || []).filter((url) => typeof url === 'string' && url.length > 0))]
}

function upsertReasoningDelta(
  current: ResearchReasoning[] | undefined,
  incoming: ResearchReasoning
): ResearchReasoning[] {
  const items = [...(current || [])]
  const index = items.findIndex((item) => item.id === incoming.id)
  if (index >= 0) {
    items[index] = {
      ...items[index],
      content: `${items[index].content}${incoming.content}`
    }
  } else {
    items.push(incoming)
  }
  return items
}

function messageText(message: Message | undefined): string {
  if (!message) return ''
  if (typeof message.content === 'string') return message.content
  return message.content
    .filter((item) => typeof item === 'object' && item !== null && 'text' in item)
    .map((item) => String((item as { text: unknown }).text || ''))
    .join('')
}

function toGraphMessage(message: ResearchMessage): Message {
  return {
    type: message.role === 'assistant' ? 'ai' : 'human',
    id: message.id,
    content: message.content
  } as Message
}

function errorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) {
    return String(error.message || '研究请求失败。')
  }
  return '研究请求失败。'
}
