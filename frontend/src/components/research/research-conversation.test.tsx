// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ResearchConversation } from './research-conversation'

afterEach(cleanup)

vi.mock('@/components/ai-elements/conversation', () => ({
  Conversation: ({ children, initial, resize }: { children: ReactNode; initial?: boolean | string; resize?: boolean | string }) => (
    <div data-testid="conversation" data-initial={String(initial)} data-resize={String(resize)}>{children}</div>
  ),
  ConversationContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ConversationScrollButton: () => null
}))

vi.mock('./research-message-item', () => ({
  ResearchMessageItem: () => <div>研究回答</div>
}))

describe('ResearchConversation', () => {
  it('首次渲染时立即定位最近记录', () => {
    render(<ResearchConversation messages={[]} onCitation={vi.fn()} />)

    expect(screen.getByTestId('conversation').dataset.initial).toBe('instant')
  })

  it('流式内容增长时立即追底，避免重叠的平滑滚动', () => {
    render(<ResearchConversation messages={[]} onCitation={vi.fn()} />)

    expect(screen.getByTestId('conversation').dataset.resize).toBe('instant')
  })
})
