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
  parseRecoveryBatch,
  type ResearchMessage,
  type ResearchProgress
} from '../domain/research'
import {
  agentProtocolStreamUrl,
  agentRequestHeaders,
  checkAgentHealth,
  loadSessionMessages,
  type RequestIdentity
} from '../services/research-api'
import {
  loadActiveRun, stopResearchRun, subscribeRunEvents,
  type ResearchRun, type RunStatus
} from '../services/research-runs'

type ResearchGraphState = {
  messages: Message[]
}

type ResearchGraphUpdate = {
  messages: Array<{ type: 'human'; id: string; content: string }>
  attachment_ids: string[]
  request_id: string
}

type AgentProtocolEvent = {
  type: 'progress' | 'final' | 'approval_required' | 'input_required' | 'run_started' | 'run_status'
  run_id?: string
  message_id?: string
  request_id?: string
  replay_after?: number
  id?: string
  stage?: string
  label?: string
  message?: string
  status?: 'started' | RunStatus
  kind?: 'status' | 'activity' | 'summary'
  activity_type?: 'web_search' | 'market_data' | 'evidence'
  group_id?: string
  task_id?: string
  task_label?: string
  query?: string
  result_count?: number
  result_urls?: string[]
  interrupt_id?: string
  interaction_id?: string
  input_type?: 'selection'
  question?: string
  recovery_batch?: unknown
  options?: Array<{
    value: string
    label: string
    description?: string | null
  }>
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
  const activeRunRef = useRef<{ runId: string; messageId?: string; terminal: boolean; replayAfter?: number } | null>(null)
  const subscriptionRef = useRef<AbortController | null>(null)
  const [isRecovering, setIsRecovering] = useState(false)
  const recoveryBusyRef = useRef(false)
  const [isStopping, setIsStopping] = useState(false)
  const [isInitializing, setIsInitializing] = useState(true)
  const initializationRef = useRef<Promise<void>>(Promise.resolve())
  const completedResponseRef = useRef(false)
  const stoppingRef = useRef(false)
  const stopCurrentRef = useRef<() => Promise<void>>(async () => undefined)
  const interactionEpochRef = useRef(0)
  const expectedRequestRef = useRef<string | null>(null)
  const sdkStopRef = useRef<() => Promise<void>>(async () => undefined)
  const identityKey = JSON.stringify([identity.tenantId, identity.userId, identity.accessToken || ''])
  const identityKeyRef = useRef(identityKey)
  identityKeyRef.current = identityKey
  // SDK 在 submit 时捕获回调。每轮赋予独立代次，旧回调不能改写后续消息。
  const callbackContext = { epoch: interactionEpochRef.current, identityKey }

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
    if (event.type === 'run_started' && event.run_id) {
      if (event.request_id && expectedRequestRef.current && event.request_id !== expectedRequestRef.current) return
      if (activeRunRef.current && event.run_id !== activeRunRef.current.runId) return
      activeRunRef.current = { runId: event.run_id, messageId: event.message_id, terminal: false, replayAfter: event.replay_after || 0 }
      if (stoppingRef.current) void stopCurrentRef.current()
      return
    }
    const assistantId = activeAssistantIdRef.current
    if (!assistantId) return
    if (event.run_id && event.run_id !== activeRunRef.current?.runId) return
    if (event.type === 'run_status' && event.status) {
      const status = event.status
      if (status !== 'running' && status !== 'stop_requested' && status !== 'started') {
        if (activeRunRef.current) activeRunRef.current.terminal = true
        completedResponseRef.current = true
        stoppingRef.current = false
        setIsStopping(false)
        setIsRecovering(false)
        recoveryBusyRef.current = false
      }
      setMessages((current) => updateMessage(current, assistantId, (message) => ({
        ...message, status: researchStatus(status),
        processStatus: status === 'stop_requested' ? '正在安全结束当前任务…' : undefined
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
        status: event.status === 'failed' || event.status === 'completed' ? event.status : 'started',
        activityType: event.activity_type,
        groupId: event.group_id,
        queryCount: event.activity_type && event.task_id ? 1 : undefined,
        resultCount: event.activity_type === 'web_search' ? (event.result_count || 0) : undefined,
        queries: event.activity_type && event.task_id ? [{
          taskId: event.task_id,
          query: event.task_label || event.query || event.message,
          status: event.status === 'failed' || event.status === 'completed' ? event.status : 'started',
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
      completedResponseRef.current = true
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
        userInput: undefined,
        evidence,
        citations: evidence.map((item) => item.index)
      })))
      return
    }
    if (
      event.type === 'input_required'
      && event.input_type === 'selection'
      && event.interrupt_id
      && event.interaction_id
      && event.question
      && event.options
      && event.options.length >= 2
    ) {
      completedResponseRef.current = true
      const hasBatch = Object.prototype.hasOwnProperty.call(event, 'recovery_batch')
      const batch = hasBatch ? parseRecoveryBatch(event.recovery_batch) : null
      const batchOptionsValid = event.options.length === 2
        && new Set(event.options.map((item) => item.value)).size === 2
        && event.options.every((item) => ['web_search', 'use_existing'].includes(item.value))
      const presentationError = hasBatch && (!batch || !batchOptionsValid)
        ? '恢复任务清单不完整或显示版本不兼容，请更新页面或重新发起请求。' : undefined
      setMessages((current) => updateMessage(current, assistantId, (message) => ({
        ...message,
        status: 'interrupted',
        processStatus: undefined,
        userInput: {
          interruptId: event.interrupt_id!,
          interactionId: event.interaction_id!,
          inputType: 'selection',
          question: event.question!,
          options: event.options!,
          ...(batch && !presentationError ? { recoveryBatch: batch } : {}),
          ...(presentationError ? { presentationError } : {})
        }
      })))
    }
  }, [])

  const recoverRun = useCallback(async (selected?: ResearchRun | null) => {
    recoveryBusyRef.current = true
    setIsRecovering(true)
    subscriptionRef.current?.abort()
    const controller = new AbortController()
    subscriptionRef.current = controller
    const currentSubscription = () => !controller.signal.aborted && subscriptionRef.current === controller && identityKeyRef.current === identityKey
    try {
      const known = activeRunRef.current
      const run = selected === undefined
        ? known ? {
          run_id: known.runId, message_id: known.messageId || activeAssistantIdRef.current || '',
          status: 'running' as const, content: '', last_event_sequence: 0, replay_after: known.replayAfter
        } : await loadActiveRun(sessionId, identity, controller.signal)
        : selected
      if (!currentSubscription()) return
      if (!run || (expectedRequestRef.current && run.request_id && run.request_id !== expectedRequestRef.current)) {
        const id = activeAssistantIdRef.current
        if (id) setMessages((current) => updateMessage(current, id, (message) => ({ ...message, status: 'error' })))
        stoppingRef.current = false
        setIsStopping(false)
        return
      }
      setIsRecovering(true)
      setError('')
      activeRunRef.current = { runId: run.run_id, messageId: run.message_id, terminal: false, replayAfter: run.replay_after || 0 }
      const assistantId = activeAssistantIdRef.current || run.message_id
      activeAssistantIdRef.current = assistantId
      const preservedContent = messagesRef.current.find((item) => item.id === assistantId)?.content || run.content
      let replayedContent = ''
      // 独立缓冲重放正文，追上现有快照后替换；断线失败也不清空已显示的内容。
      setMessages((current) => {
        const message: ResearchMessage = {
          id: assistantId, role: 'assistant', content: preservedContent, status: researchStatus(run.status),
          createdAt: new Date().toISOString(), progress: [], processStatus: '正在恢复回答…'
        }
        return current.some((item) => item.id === assistantId)
          ? updateMessage(current, assistantId, (existing) => ({
            ...message, content: existing.content || run.content,
            status: stoppingRef.current ? 'stopping' : message.status
          })) : [...current, message]
      })
      let cursor = run.replay_after || 0
      let failures = 0
      while (currentSubscription() && !activeRunRef.current?.terminal) {
        try {
          await subscribeRunEvents(sessionId, run.run_id, identity, cursor, controller.signal, (frame) => {
            if (!currentSubscription() || activeRunRef.current?.runId !== run.run_id) return
            cursor = frame.sequence
            if (frame.event === 'custom') {
              const event = frame.data as AgentProtocolEvent
              if (event.type === 'run_started' && event.run_id === run.run_id) replayedContent = ''
              handleCustomEvent(event)
            }
            if (frame.event === 'messages') {
              const chunk = Array.isArray(frame.data) ? frame.data[0] : undefined
              const content = chunk && typeof chunk.content === 'string' ? chunk.content : ''
              if (content && !stoppingRef.current && !activeRunRef.current?.terminal) {
                replayedContent += content
                if (replayedContent.length >= preservedContent.length) {
                  setMessages((current) => updateMessage(current, assistantId, (message) => ({ ...message, content: replayedContent })))
                }
              }
            }
            if (frame.event === 'error') {
              const data = frame.data as { message?: string }
              setError(data.message || '任务执行失败')
            }
          })
          if (activeRunRef.current?.terminal) break
          throw new Error('输出连接已断开，正在重连')
        } catch (caught) {
          if (!currentSubscription()) return
          failures += 1
          if (failures >= 5) throw caught
          await new Promise<void>((resolve) => {
            const timer = window.setTimeout(done, Math.min(failures * 1000, 4000))
            function done() { clearTimeout(timer); controller.signal.removeEventListener('abort', done); resolve() }
            controller.signal.addEventListener('abort', done, { once: true })
          })
        }
      }
    } catch (caught) {
      if (currentSubscription()) setError(`${errorMessage(caught)}，请刷新重连；后台任务状态未被修改。`)
    } finally {
      if (currentSubscription()) {
        recoveryBusyRef.current = false
        setIsRecovering(false)
      }
    }
  }, [sessionId, identityKey, handleCustomEvent])

  const stream = useStream<ResearchGraphState, ResearchStreamBag>({
    transport,
    threadId: sessionId,
    throttle: true,
    initialValues: { messages: [] },
    onCustomEvent: (event) => {
      if (callbackContext.epoch === interactionEpochRef.current && callbackContext.identityKey === identityKeyRef.current) handleCustomEvent(event)
    },
    onError: (caught) => {
      if (callbackContext.epoch !== interactionEpochRef.current || callbackContext.identityKey !== identityKeyRef.current) return
      if (activeRunRef.current?.terminal) return
      const message = errorMessage(caught)
      setError(message)
      if ((activeRunRef.current && !activeRunRef.current.terminal) || !completedResponseRef.current) {
        void recoverRun()
        return
      }
      if (completedResponseRef.current) return
      const assistantId = activeAssistantIdRef.current
      if (assistantId) {
        setMessages((current) => updateMessage(current, assistantId, (item) => ({
          ...item,
          content: item.content || message,
          status: item.userInput ? 'interrupted' : 'error',
          userInput: item.userInput ? { ...item.userInput, isSubmitting: false } : undefined
        })))
      }
    },
    onFinish: () => {
      if (callbackContext.epoch !== interactionEpochRef.current || callbackContext.identityKey !== identityKeyRef.current) return
      if ((activeRunRef.current && !activeRunRef.current.terminal) || !completedResponseRef.current) {
        void recoverRun()
        return
      }
      const assistantId = activeAssistantIdRef.current
      if (assistantId) {
        setMessages((current) => updateMessage(current, assistantId, (message) => ({
          ...message,
          status: message.status === 'error' || message.status === 'interrupted' || message.status === 'cancelled' || message.status === 'stopping'
            ? message.status
            : 'complete',
          userInput: message.userInput
            ? { ...message.userInput, isSubmitting: false }
            : undefined
        })))
      }
      activeAssistantIdRef.current = null
      streamMessageIdsAtSubmitRef.current.clear()
    }
  })
  sdkStopRef.current = stream.stop

  useEffect(() => {
    let disposed = false
    const epoch = interactionEpochRef.current
    setIsInitializing(true)
    void checkAgentHealth().then(setIsOnline)
    initializationRef.current = loadSessionMessages(sessionId, identity).then((items) => {
      if (disposed || epoch !== interactionEpochRef.current) return
      const conversation = toConversationMessages(items)
      const restored = conversation.map<ResearchMessage>((item) => ({
        id: item.messageId || crypto.randomUUID(),
        role: item.role === 'agent' ? 'assistant' : 'user',
        content: item.content,
        createdAt: new Date().toISOString(),
        status: researchStatus(item.status || 'completed'),
        progress: restoreResearchProgress(item.researchProcess)
      }))
      if (restored.length) setMessages(restored)
      return loadActiveRun(sessionId, identity).then((run) => {
        if (disposed || epoch !== interactionEpochRef.current) return
        if (run) {
          void recoverRun(run)
          return
        }
        // 历史与 active 不是同一个快照；两次读取间任务可能已结束并清空 active。
        const latestAssistant = [...conversation].reverse().find((item) => item.role === 'agent')
        if (latestAssistant?.runId && latestAssistant.messageId
          && ['pending', 'streaming', 'running', 'stop_requested'].includes(latestAssistant.status || '')) {
          void recoverRun({
            run_id: latestAssistant.runId,
            message_id: latestAssistant.messageId,
            status: latestAssistant.status === 'stop_requested' ? 'stop_requested' : 'running',
            content: latestAssistant.content,
            last_event_sequence: 0
          })
        }
      })
    }).catch((caught) => {
      if (!disposed) setError(`恢复会话失败：${errorMessage(caught)}`)
    }).finally(() => { if (!disposed) setIsInitializing(false) })
    return () => {
      disposed = true
      interactionEpochRef.current += 1
      subscriptionRef.current?.abort()
      void sdkStopRef.current()
      activeAssistantIdRef.current = null
      activeRunRef.current = null
      expectedRequestRef.current = null
      completedResponseRef.current = false
      stoppingRef.current = false
      setIsStopping(false)
      setIsRecovering(false)
      recoveryBusyRef.current = false
      setMessages([welcomeMessage])
    }
  }, [identity.tenantId, identity.userId, identity.accessToken, sessionId, recoverRun])

  useEffect(() => {
    const assistantId = activeAssistantIdRef.current
    if (!assistantId || isRecovering || stoppingRef.current || activeRunRef.current?.terminal) return
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
    setMessages((current) => updateMessage(current, assistantId, (message) => (
      message.status === 'complete' ? message : { ...message, content }
    )))
  }, [stream.messages, isRecovering])

  const sendMessage = useCallback(async (content: string, attachmentIds: string[] = []) => {
    const submitIdentity = identityKey
    await initializationRef.current
    if (submitIdentity !== identityKeyRef.current) return
    if (!content.trim() || stream.isLoading || recoveryBusyRef.current || isRecovering || stoppingRef.current) return
    if (activeRunRef.current && !activeRunRef.current.terminal) return
    interactionEpochRef.current += 1
    callbackContext.epoch = interactionEpochRef.current
    subscriptionRef.current?.abort()
    activeRunRef.current = null
    const requestId = `req_${crypto.randomUUID()}`
    expectedRequestRef.current = requestId
    completedResponseRef.current = false
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
      progress: [],
      processStatus: '正在理解问题与研究范围。',
      createdAt: new Date().toISOString(),
      status: 'streaming'
    }])
    await stream.submit(
      {
        messages: [{ type: 'human', id: userMessage.id, content: text }],
        attachment_ids: attachmentIds,
        request_id: requestId
      },
      {
        optimisticValues: {
          messages: optimisticMessages
        }
      }
    )
  }, [stream, isRecovering])

  const cancelMessage = useCallback(async () => {
    if (isInitializing && !activeAssistantIdRef.current) return
    const epoch = interactionEpochRef.current
    const stopIdentity = identityKey
    const isCurrentStop = () => epoch === interactionEpochRef.current && stopIdentity === identityKeyRef.current
    stoppingRef.current = true
    setIsStopping(true)
    const assistantId = activeAssistantIdRef.current
    if (assistantId) setMessages((current) => updateMessage(current, assistantId, (item) => ({ ...item, status: 'stopping', processStatus: '正在安全结束当前任务…' })))
    try {
      let runId = activeRunRef.current?.runId
      if (!runId) {
        const run = await loadActiveRun(sessionId, identity)
        if (!isCurrentStop()) return
        runId = run?.run_id
        if (run) activeRunRef.current = { runId: run.run_id, messageId: run.message_id, terminal: false }
      }
      // 若尚未收到 accepted，收到 run_started 后自动补发停止请求。
      if (!runId) return
      const result = await stopResearchRun(sessionId, runId, identity)
      if (!isCurrentStop() || activeRunRef.current?.runId !== runId || activeRunRef.current.terminal) return
      handleCustomEvent({ type: 'run_status', run_id: runId, status: result.status })
      if (result.status === 'stop_requested') return
      subscriptionRef.current?.abort()
      await stream.stop()
    } catch (caught) {
      if (!isCurrentStop() || activeRunRef.current?.terminal) return
      stoppingRef.current = false
      setIsStopping(false)
      setError(`停止未确认：${errorMessage(caught)}`)
      if (assistantId) setMessages((current) => updateMessage(current, assistantId, (item) => ({ ...item, status: 'streaming' })))
    }
  }, [stream, sessionId, identity.tenantId, identity.userId, identity.accessToken, handleCustomEvent])
  stopCurrentRef.current = cancelMessage

  const resumeUserInput = useCallback(async (messageId: string, value: string) => {
    if (stream.isLoading || isRecovering || isStopping || isInitializing) return
    const message = messagesRef.current.find((item) => item.id === messageId)
    const userInput = message?.userInput
    if (!userInput || userInput.isSubmitting || userInput.presentationError) return
    if (!userInput.options.some((option) => option.value === value)) return

    interactionEpochRef.current += 1
    callbackContext.epoch = interactionEpochRef.current
    expectedRequestRef.current = null
    completedResponseRef.current = false
    if (activeRunRef.current) activeRunRef.current.terminal = false

    setError('')
    activeAssistantIdRef.current = messageId
    lastStreamedAnswerRef.current = null
    setMessages((current) => updateMessage(current, messageId, (item) => ({
      ...item,
      status: 'interrupted',
      userInput: item.userInput ? {
        ...item.userInput,
        isSubmitting: true,
        selectedValue: value
      } : undefined
    })))
    try {
      await stream.submit(null, {
        command: {
          resume: {
            interrupt_id: userInput.interruptId,
            interaction_id: userInput.interactionId,
            input_type: userInput.inputType,
            value,
            ...(userInput.recoveryBatch ? {
              presentation_version: userInput.recoveryBatch.version,
              batch_digest: userInput.recoveryBatch.digest
            } : {})
          }
        }
      })
    } finally {
      setMessages((current) => updateMessage(current, messageId, (item) => ({
        ...item,
        userInput: item.userInput ? { ...item.userInput, isSubmitting: false } : undefined
      })))
    }
  }, [stream])

  return {
    sessionId,
    messages,
    isStreaming: stream.isLoading || isRecovering || isStopping || isInitializing,
    isOnline,
    error,
    sendMessage,
    resumeUserInput,
    cancelMessage
  }
}

function researchStatus(status: string): ResearchMessage['status'] {
  if (status === 'cancelled') return 'cancelled'
  if (status === 'failed') return 'error'
  if (status === 'interrupted') return 'interrupted'
  if (status === 'stop_requested') return 'stopping'
  if (status === 'running' || status === 'pending' || status === 'streaming') return 'streaming'
  return 'complete'
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
