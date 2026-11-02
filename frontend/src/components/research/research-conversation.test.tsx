// @vitest-environment jsdom

import { act, cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ResearchConversation } from './research-conversation'

afterEach(() => { cleanup(); vi.useRealTimers() })

const scrollToBottom = vi.hoisted(() => vi.fn())
vi.mock('use-stick-to-bottom', () => ({
  useStickToBottomContext: () => ({ scrollToBottom, stopScroll: vi.fn(), scrollRef: { current: null } })
}))

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
  it('定位高亮停留后消失，重复定位同一消息可重新播放且不重建消息', () => {
    vi.useFakeTimers()
    const messages = [{ id: 'u1', role: 'user' as const, content: '目标消息', createdAt: '', status: 'complete' as const }]
    const { container, rerender } = render(<ResearchConversation messages={messages} navigationTarget={{ messageId: 'u1', requestId: 'jump1' }} onCitation={vi.fn()} />)
    const anchor = container.querySelector('[data-message-id="u1"]')
    const firstHighlight = container.querySelector('.research-location-highlight')
    expect(firstHighlight).toBeTruthy()
    act(() => vi.advanceTimersByTime(1800))
    expect(container.querySelector('.research-location-highlight')).toBe(firstHighlight)
    act(() => vi.advanceTimersByTime(800))
    expect(container.querySelector('.research-location-highlight')).toBeNull()
    expect(anchor?.classList.contains('research-message-anchor--located')).toBe(false)
    rerender(<ResearchConversation messages={messages} navigationTarget={{ messageId: 'u1', requestId: 'jump2' }} onCitation={vi.fn()} />)
    expect(container.querySelector('.research-location-highlight')).toBeTruthy()
    expect(container.querySelector('[data-message-id="u1"]')).toBe(anchor)
  })
  it('新提问渲染后复用底部按钮滚动，助手更新不打断上翻阅读', () => {
    scrollToBottom.mockClear()
    const { rerender } = render(<ResearchConversation messages={[]} onCitation={vi.fn()} />)
    const messages = [{ id: 'u1', role: 'user' as const, content: '新问题', createdAt: '', status: 'complete' as const }]
    rerender(<ResearchConversation messages={messages} submittedMessageId="u1" onCitation={vi.fn()} />)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)
    rerender(<ResearchConversation messages={[...messages, { id: 'a1', role: 'assistant', content: '正在生成', createdAt: '', status: 'streaming' }]} submittedMessageId="u1" onCitation={vi.fn()} />)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)
  })
  it('首次渲染时立即定位最近记录', () => {
    render(<ResearchConversation messages={[]} onCitation={vi.fn()} />)

    expect(screen.getByTestId('conversation').dataset.initial).toBe('instant')
  })

  it('流式内容增长时立即追底，避免重叠的平滑滚动', () => {
    render(<ResearchConversation messages={[]} onCitation={vi.fn()} />)

    expect(screen.getByTestId('conversation').dataset.resize).toBe('instant')
  })
})
