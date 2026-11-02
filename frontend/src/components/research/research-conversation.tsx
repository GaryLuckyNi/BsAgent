import { Conversation, ConversationContent, ConversationScrollButton } from '@/components/ai-elements/conversation'
import type { ResearchMessage } from '@/domain/research'
import { useEffect, useState, type Ref } from 'react'
import type { StickToBottomContext } from 'use-stick-to-bottom'
import { useStickToBottomContext } from 'use-stick-to-bottom'

import { ResearchMessageItem } from './research-message-item'

export function ResearchConversation({ messages, submittedMessageId, navigationTarget, navigationStatus, contextRef, onCitation, onUserInputSelect }: {
  messages: ResearchMessage[]
  submittedMessageId?: string
  navigationTarget?: { messageId: string; requestId: string }
  navigationStatus?: string
  contextRef?: Ref<StickToBottomContext>
  onCitation: (message: ResearchMessage, index: number) => void
  onUserInputSelect?: (messageId: string, value: string) => void
}) {
  const [highlightedRequest, setHighlightedRequest] = useState<string>()
  useEffect(() => {
    setHighlightedRequest(navigationTarget?.requestId)
    if (!navigationTarget) return
    const timer = window.setTimeout(() => setHighlightedRequest(undefined), 2600)
    return () => clearTimeout(timer)
  }, [navigationTarget?.requestId])

  return (
    <Conversation
      className="conversation"
      contextRef={contextRef}
      initial="instant"
      resize="instant"
    >
      <ConversationContent className="conversation-content">
        {messages.map((message) => (
          <div key={message.id} data-message-id={message.id} tabIndex={-1}
            className={navigationTarget?.messageId === message.id && highlightedRequest === navigationTarget.requestId ? 'research-message-anchor research-message-anchor--located' : 'research-message-anchor'}>
            {navigationTarget?.messageId === message.id && highlightedRequest === navigationTarget.requestId && <div key={navigationTarget.requestId} className="research-location-highlight" aria-hidden="true" />}
            <ResearchMessageItem
              message={message}
              onCitation={onCitation}
              onUserInputSelect={onUserInputSelect}
            />
          </div>
        ))}
      </ConversationContent>
      <ScrollAfterSubmit messageId={submittedMessageId} />
      <ScrollToSearchResult target={navigationTarget} />
      {navigationStatus && <div className="research-navigation-status" role="status">{navigationStatus}</div>}
      <ConversationScrollButton aria-label="滚动到最下方" />
    </Conversation>
  )
}

function ScrollToSearchResult({ target }: { target?: { messageId: string; requestId: string } }) {
  const { stopScroll, scrollRef } = useStickToBottomContext()
  useEffect(() => {
    if (!target) return
    const container = scrollRef.current
    const element = container && [...container.querySelectorAll<HTMLElement>('[data-message-id]')].find((item) => item.dataset.messageId === target.messageId)
    if (!container || !element) return
    stopScroll()
    container.scrollTo({ top: element.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop - 20, behavior: 'instant' })
    element.focus({ preventScroll: true })
  }, [target?.requestId, scrollRef, stopScroll])
  return null
}

function ScrollAfterSubmit({ messageId }: { messageId?: string }) {
  const { scrollToBottom } = useStickToBottomContext()
  useEffect(() => {
    if (messageId) void scrollToBottom()
  }, [messageId, scrollToBottom])
  return null
}
