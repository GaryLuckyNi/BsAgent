import { Conversation, ConversationContent, ConversationScrollButton } from '@/components/ai-elements/conversation'
import type { ResearchMessage } from '@/domain/research'

import { ResearchMessageItem } from './research-message-item'

export function ResearchConversation({ messages, onCitation }: {
  messages: ResearchMessage[]
  onCitation: (message: ResearchMessage, index: number) => void
}) {
  return (
    <Conversation
      key={messages[0]?.id ?? "empty"}
      className="conversation"
      initial="instant"
      resize="instant"
    >
      <ConversationContent className="conversation-content">
        {messages.map((message) => <ResearchMessageItem key={message.id} message={message} onCitation={onCitation} />)}
      </ConversationContent>
      <ConversationScrollButton />
    </Conversation>
  )
}
