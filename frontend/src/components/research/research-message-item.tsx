import {
  CheckCircle2,
  CircleAlert,
  Compass,
  Copy,
  Database,
  LoaderCircle,
  Search,
  ThumbsDown,
  ThumbsUp,
  Workflow
} from 'lucide-react'

import {
  Message,
  MessageAction,
  MessageActions,
  MessageContent,
  MessageResponse
} from '@/components/ai-elements/message'
import {
  Reasoning,
  ReasoningContent,
  ReasoningText,
  ReasoningTrigger
} from '@/components/ai-elements/reasoning'
import type { ResearchMessage, ResearchProgress } from '@/domain/research'

export function ResearchMessageItem({ message, onCitation }: {
  message: ResearchMessage
  onCitation: (message: ResearchMessage, index: number) => void
}) {
  const seenNarratives = new Set<string>()
  const processEntries: ResearchProgress[] = []
  for (const item of message.progress || []) {
    const isProcessEntry = item.kind === 'summary'
      || item.kind === 'activity'
      || (!item.kind && (item.stage === 'tool_agent' || item.stage === 'skill_agent'))
    if (!isProcessEntry) continue
    if (item.kind === 'summary') {
      const narrative = normalizeNarrative(item.message)
      if (seenNarratives.has(narrative)) continue
      seenNarratives.add(narrative)
    }
    processEntries.push(item)
  }
  processEntries.sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0))
  const hasResearchProcess = processEntries.length > 0
  const isResearching = message.status === 'streaming' && !message.content
  const showStreamLoader = message.role === 'assistant'
    && message.status === 'streaming'
    && !message.content
    && !hasResearchProcess
  const statusLabel = message.status === 'streaming'
    ? '正在研究'
    : message.status === 'error'
      ? '研究失败'
      : '研究完成'

  return (
    <Message from={message.role} className={`message message--${message.role}`}>
      {message.role === 'assistant' && <div className="assistant-avatar"><Compass size={17} /></div>}
      <MessageContent className="message-body">
        {message.role === 'assistant' && <div className="message-author">InvestWise 研究助手 <span>{statusLabel}</span></div>}
        {hasResearchProcess && (
          <Reasoning className="reasoning" isStreaming={isResearching} autoClose={false}>
            {!isResearching && (
              <ReasoningTrigger className="reasoning-trigger" />
            )}
            {hasResearchProcess && (
              <ReasoningContent>
                <div className="reasoning-stream">
                  {processEntries.map((item) => (
                    item.kind === 'summary'
                  ) ? (
                    <div className="reasoning-narrative" key={`reasoning:${item.id}`}>
                      <ReasoningText>{item.message}</ReasoningText>
                    </div>
                  ) : (
                    <div
                      className="reasoning-activity"
                      aria-label="研究活动"
                      data-status={item.status}
                      key={`activity:${item.id}`}
                    >
                      <ActivityIcon
                        activityType={item.activityType}
                        message={item.message}
                        status={item.status}
                      />
                      <div className="reasoning-activity__content">
                        <span>{activityMessage(item)}</span>
                        {item.activityType && item.queries?.length ? (
                          <details className="reasoning-activity__details">
                            <summary>{item.activityType === 'web_search' ? '查看搜索范围' : '查看任务明细'}</summary>
                            <ul>
                              {item.queries.map((query) => (
                                <li key={query.taskId}>{query.query}</li>
                              ))}
                            </ul>
                          </details>
                        ) : null}
                      </div>
                    </div>
                  ))}
                </div>
              </ReasoningContent>
            )}
          </Reasoning>
        )}
        {showStreamLoader && (
          <span className="response-stream-loader" role="status" aria-label="回复生成中">
            <span /><span /><span />
          </span>
        )}
        {message.content && (
          <MessageResponse className="message-content" isAnimating={message.status === 'streaming'}>{message.content}</MessageResponse>
        )}
        {message.citations && message.citations.length > 0 && (
          <div className="citation-row">
            <span>引用证据</span>
            {message.citations.map((index) => <button key={index} type="button" onClick={() => onCitation(message, index)} aria-label={`打开第 ${index} 条证据`}>{index}</button>)}
          </div>
        )}
        {message.role === 'assistant' && message.status !== 'streaming' && (
          <MessageActions className="message-actions">
            <MessageAction tooltip="复制回答" onClick={() => void navigator.clipboard.writeText(message.content)}><Copy size={14} /></MessageAction>
            <MessageAction tooltip="有帮助"><ThumbsUp size={14} /></MessageAction>
            <MessageAction tooltip="无帮助"><ThumbsDown size={14} /></MessageAction>
          </MessageActions>
        )}
      </MessageContent>
    </Message>
  )
}

function activityMessage(item: ResearchProgress): string {
  const count = item.queryCount || 1
  if (item.activityType === 'web_search') {
    if (item.status === 'started') return `正在搜索 ${count} 项信息…`
    if (item.status === 'failed') return '搜索未完成'
    return `搜索到 ${item.resultCount || 0} 个网页`
  }
  if (item.activityType === 'market_data') {
    if (item.status === 'started') return `正在查询 ${count} 项市场数据…`
    if (item.status === 'failed') return '市场数据查询未完成'
    return `已取得 ${count} 项市场数据`
  }
  if (item.activityType === 'evidence') {
    if (item.status === 'started') return `正在获取 ${count} 项研究证据…`
    if (item.status === 'failed') return '研究证据获取未完成'
    return `已取得 ${count} 项研究证据`
  }
  return item.message
}

function ActivityIcon({ activityType, message, status }: {
  activityType?: ResearchProgress['activityType']
  message: string
  status: 'started' | 'completed' | 'failed'
}) {
  if (status === 'failed') return <CircleAlert size={15} aria-hidden="true" />
  if (status === 'completed') return <CheckCircle2 size={15} aria-hidden="true" />
  if (activityType === 'web_search' || message.includes('联网检索')) return <Search size={15} aria-hidden="true" />
  if (activityType === 'market_data' || message.includes('市场数据')) return <Database size={15} aria-hidden="true" />
  if (message.includes('研究流程')) return <Workflow size={15} aria-hidden="true" />
  return <LoaderCircle className="reasoning-activity__spinner" size={15} aria-hidden="true" />
}

function normalizeNarrative(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}
