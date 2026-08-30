// @vitest-environment jsdom

import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ResearchConversation } from './research-conversation'

vi.mock('@/components/ai-elements/conversation', () => ({
  Conversation: ({ children, initial }: { children: ReactNode; initial?: boolean | string }) => (
    <div data-testid="conversation" data-initial={String(initial)}>{children}</div>
  ),
  ConversationContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ConversationScrollButton: () => null
}))

vi.mock('./research-message-item', () => ({
  ResearchMessageItem: () => <div>研究回答</div>
}))

describe('ResearchConversation', () => {
  it('首次渲染时不自动滚动到回答底部', () => {
    render(<ResearchConversation messages={[]} onCitation={vi.fn()} />)

    expect(screen.getByTestId('conversation').dataset.initial).toBe('false')
  })
})
