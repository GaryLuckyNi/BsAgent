import { Compass, Copy, ThumbsDown, ThumbsUp } from 'lucide-react'

import {
  Message,
  MessageAction,
  MessageActions,
  MessageContent,
  MessageResponse
} from '@/components/ai-elements/message'
import { Reasoning, ReasoningContent, ReasoningTrigger } from '@/components/ai-elements/reasoning'
import type { ResearchMessage } from '@/domain/research'

export function ResearchMessageItem({ message, onCitation }: {
  message: ResearchMessage
  onCitation: (message: ResearchMessage, index: number) => void
}) {
  return (
    <Message from={message.role} className={`message message--${message.role}`}>
      {message.role === 'assistant' && <div className="assistant-avatar"><Compass size={17} /></div>}
      <MessageContent className="message-body">
        {message.role === 'assistant' && <div className="message-author">InvestWise 研究助手 <span>{message.status === 'streaming' ? '正在研究' : '研究完成'}</span></div>}
        {message.thinking && (
          <Reasoning className="reasoning" isStreaming={message.status === 'streaming'}>
            <ReasoningTrigger />
            <ReasoningContent>{message.thinking}</ReasoningContent>
          </Reasoning>
        )}
        <MessageResponse className="message-content">{message.content || '正在整理研究结果…'}</MessageResponse>
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
