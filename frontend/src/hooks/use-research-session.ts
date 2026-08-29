import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { resolveConversationSession, toConversationMessages } from '../conversation-session'
import { buildEvidenceIndex, type ResearchMessage } from '../domain/research'
import {
  checkAgentHealth,
  loadSessionMessages,
  streamResearchMessage,
  type RequestIdentity
} from '../services/research-api'

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
  const [isStreaming, setIsStreaming] = useState(false)
  const [isOnline, setIsOnline] = useState<boolean | null>(null)
  const [error, setError] = useState('')
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    void checkAgentHealth().then(setIsOnline)
    void loadSessionMessages(sessionId, identity).then((items) => {
      if (!items.length) return
      const restored = toConversationMessages(items).map<ResearchMessage>((item) => ({
        id: crypto.randomUUID(),
        role: item.role === 'agent' ? 'assistant' : 'user',
        content: item.content,
        createdAt: new Date().toISOString(),
        status: 'complete'
      }))
      setMessages(restored)
    }).catch(() => undefined)
  }, [identity, sessionId])

  const sendMessage = useCallback(async (content: string, attachmentIds: string[] = []) => {
    if (!content.trim() || isStreaming) return
    setError('')
    const userMessage: ResearchMessage = {
      id: crypto.randomUUID(), role: 'user', content: content.trim(),
      createdAt: new Date().toISOString(), status: 'complete'
    }
    const assistantId = crypto.randomUUID()
    setMessages((current) => [...current, userMessage, {
      id: assistantId, role: 'assistant', content: '', thinking: '',
      createdAt: new Date().toISOString(), status: 'streaming'
    }])
    setIsStreaming(true)
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const response = await streamResearchMessage(
        sessionId, content.trim(), attachmentIds, identity,
        {
          onThinking: (delta) => setMessages((current) => updateMessage(current, assistantId, (message) => ({ ...message, thinking: `${message.thinking ?? ''}${delta}` }))),
          onProgress: (progress) => setMessages((current) => updateMessage(current, assistantId, (message) => ({ ...message, thinking: message.thinking || progress.message }))),
          onAnswer: (delta) => setMessages((current) => updateMessage(current, assistantId, (message) => ({ ...message, content: `${message.content}${delta}` })))
        }, controller.signal
      )
      const evidence = buildEvidenceIndex(response.cited_evidence_ids.map((id) => ({
        id, publisher: '投资研究数据源', title: `引用资料 ${id}`,
        url: '#', summary: '原始资料地址将由后端证据元数据补充。'
      })))
      setMessages((current) => updateMessage(current, assistantId, (message) => ({
        ...message, content: response.content || message.content, status: 'complete',
        evidence, citations: evidence.map((item) => item.index)
      })))
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === 'AbortError')) setError(errorMessage(caught))
      setMessages((current) => updateMessage(current, assistantId, (message) => ({ ...message, status: 'error' })))
    } finally {
      setIsStreaming(false)
      abortRef.current = null
    }
  }, [identity, isStreaming, sessionId])

  const cancelMessage = useCallback(() => abortRef.current?.abort(), [])
  return { sessionId, messages, isStreaming, isOnline, error, sendMessage, cancelMessage }
}

function updateMessage(messages: ResearchMessage[], id: string, update: (message: ResearchMessage) => ResearchMessage) {
  return messages.map((message) => message.id === id ? update(message) : message)
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '研究请求失败。'
}
