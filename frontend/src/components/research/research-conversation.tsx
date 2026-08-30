import { Conversation, ConversationContent, ConversationScrollButton } from '@/components/ai-elements/conversation'
import type { ResearchMessage } from '@/domain/research'

import { ResearchMessageItem } from './research-message-item'

export function ResearchConversation({ messages, onCitation }: {
  messages: ResearchMessage[]
  onCitation: (message: ResearchMessage, index: number) => void
}) {
  return (
    <Conversation className="conversation">
      <ConversationContent className="conversation-content">
        {messages.map((message) => <ResearchMessageItem key={message.id} message={message} onCitation={onCitation} />)}
      </ConversationContent>
      <ConversationScrollButton />
    </Conversation>
  )
}
