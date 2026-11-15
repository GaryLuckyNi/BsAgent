// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { AiResearchView } from './index'

const api = vi.hoisted(() => ({ list: vi.fn(), messages: vi.fn(), pages: vi.fn(), stop: vi.fn(), submit: vi.fn(), stopScroll: vi.fn(), scroll: vi.fn() }))
vi.mock('react-router-dom', () => ({ useOutletContext: () => ({
  authentication: { identity: { tenantId: 't1', userId: 'u1' }, error: '' }
}) }))
vi.mock('@/services/research-api', () => ({
  loadResearchSessions: api.list, loadSessionMessages: api.messages,
  checkAgentHealth: async () => true, agentProtocolStreamUrl: (id: string) => `/stream/${id}`,
  agentRequestHeaders: () => ({})
}))
vi.mock('@/services/research-runs', () => ({ loadActiveRun: async () => null }))
vi.mock('@/services/research-history-pages', () => ({ loadResearchHistoryPage: api.pages }))
vi.mock('@/components/research/research-search-dialog', () => ({ ResearchSearchDialog: ({ open, onSelect }: { open: boolean; onSelect: (result: unknown) => void }) => open ? <button onClick={() => onSelect({ session_id: 's2', message_id: 'm100', sequence_no: 100, title: '真实研究二', snippet: '历史目标正文', created_at: '2026-10-20T01:00:00Z' })}>命中较早消息</button> : null }))
vi.mock('@langchain/langgraph-sdk/react', () => ({
  FetchStreamTransport: class {},
  useStream: () => ({ messages: [], isLoading: false, submit: api.submit, stop: api.stop })
}))
vi.mock('@/components/ai-elements/conversation', () => ({
  Conversation: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ConversationContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ConversationScrollButton: () => null
}))
vi.mock('use-stick-to-bottom', () => ({ useStickToBottomContext: () => ({ scrollToBottom: () => {}, stopScroll: api.stopScroll, scrollRef: { current: document.body } }) }))
vi.mock('@/components/research/research-message-item', () => ({
  ResearchMessageItem: ({ message }: { message: { content: string } }) => <article>{message.content}</article>
}))

const records = [
  { session_id: 's1', title: '真实研究一', preview: '第一段实际摘要', updated_at: '2026-10-28T02:00:00Z' },
  { session_id: 's2', title: '真实研究二', preview: '第二段实际摘要', updated_at: '2026-10-28T01:00:00Z' }
]
function history(content: string) {
  return [{ message_id: content, role: 'user', status: 'completed', content, sequence_no: 1, metadata: {}, created_at: '2026-10-28T01:00:00Z' }]
}

beforeEach(() => {
  const values = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', { configurable: true, value: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value)
  } })
  api.list.mockReset().mockResolvedValue(records)
  api.messages.mockReset().mockImplementation(async (id: string) => id === 's1' ? history('会话一正文') : id === 's2' ? history('会话二正文') : [])
  api.stop.mockReset()
  api.submit.mockReset().mockResolvedValue(undefined)
  api.pages.mockReset().mockResolvedValue([])
  api.stopScroll.mockReset()
  api.scroll.mockReset()
  document.body.scrollTo = api.scroll
})
afterEach(cleanup)

describe('真实研究页面', () => {
  it('收起不重载会话或丢失草稿，重新挂载记住侧栏状态', async () => {
    const { container, unmount } = render(<TooltipProvider><AiResearchView /></TooltipProvider>)
    const composer = screen.getByRole('textbox', { name: '研究问题' })
    fireEvent.change(composer, { target: { value: '未发送的草稿' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit' }).hasAttribute('disabled')).toBe(false))
    const requests = api.messages.mock.calls.length
    const stops = api.stop.mock.calls.length
    fireEvent.click(within(container.querySelector('.desktop-sidebar') as HTMLElement).getByRole('button', { name: '收起侧栏' }))
    expect(container.querySelector('.workspace--sidebar-collapsed')).toBeTruthy()
    expect((composer as HTMLTextAreaElement).value).toBe('未发送的草稿')
    expect(api.messages).toHaveBeenCalledTimes(requests)
    expect(api.stop).toHaveBeenCalledTimes(stops)
    unmount()
    const mounted = render(<TooltipProvider><AiResearchView /></TooltipProvider>)
    expect(mounted.container.querySelector('.workspace--sidebar-collapsed')).toBeTruthy()
  })
  it('跨会话搜索定位等待首次恢复，再补齐旧消息，避免初始化覆盖目标', async () => {
    let finishInitial!: (items: ReturnType<typeof history>) => void
    const page = (start: number, end: number) => Array.from({ length: end - start + 1 }, (_, i) => ({
      ...history(`正文${start + i}`)[0], message_id: `m${start + i}`, sequence_no: start + i
    }))
    api.messages.mockImplementation((id: string) => id === 's2' ? new Promise((resolve) => { finishInitial = resolve }) : Promise.resolve([]))
    api.pages.mockResolvedValueOnce(page(201, 400)).mockResolvedValueOnce(page(1, 200))
    const { container } = render(<TooltipProvider><AiResearchView /></TooltipProvider>)
    fireEvent.click(within(container.querySelector('.desktop-sidebar') as HTMLElement).getByRole('button', { name: '搜索研究记录' }))
    fireEvent.click(screen.getByRole('button', { name: '命中较早消息' }))
    await waitFor(() => expect(finishInitial).toBeTypeOf('function'))
    expect(api.pages).not.toHaveBeenCalled()
    await act(async () => finishInitial(page(401, 600)))
    expect(await screen.findByText('正文100')).toBeTruthy()
    expect(screen.getByText('正文600')).toBeTruthy()
    await waitFor(() => expect(document.activeElement?.getAttribute('data-message-id')).toBe('m100'))
    expect(api.pages.mock.calls.map((call) => call[2])).toEqual([401, 201])
    expect(api.stopScroll).toHaveBeenCalled()
    expect(api.scroll).toHaveBeenCalled()
  })
  it('恢复已有会话的回答终态后也刷新真实摘要', async () => {
    api.list.mockResolvedValueOnce(records).mockResolvedValue([
      { ...records[0], preview: '恢复完成后的真实摘要', updated_at: '2026-10-28T04:00:00Z' }, records[1]
    ])
    api.messages.mockImplementation(async (id: string) => id === 's1' ? [
      ...history('原问题'), { ...history('恢复完成的正文')[0], role: 'assistant', sequence_no: 2 }
    ] : [])
    const { container } = render(<TooltipProvider><AiResearchView /></TooltipProvider>)
    const sidebar = within(container.querySelector('.desktop-sidebar') as HTMLElement)
    fireEvent.click(await sidebar.findByRole('button', { name: /真实研究一/ }))
    expect(await screen.findByText('恢复完成的正文')).toBeTruthy()
    await waitFor(() => expect(container.querySelector('.research-header p')?.textContent).toBe('恢复完成后的真实摘要'))
  })
  it('选择记录同时切换真实标题、摘要与消息，新建后清空原会话', async () => {
    const { container } = render(<TooltipProvider><AiResearchView /></TooltipProvider>)
    const sidebar = () => within(container.querySelector('.desktop-sidebar') as HTMLElement)
    fireEvent.click(await sidebar().findByRole('button', { name: /真实研究一/ }))
    expect(await screen.findByRole('heading', { name: '真实研究一' })).toBeTruthy()
    expect(await screen.findByText('会话一正文')).toBeTruthy()
    expect(container.querySelector('.research-header p')?.textContent).toBe('第一段实际摘要')
    fireEvent.click(sidebar().getByRole('button', { name: /真实研究二/ }))
    expect(await screen.findByText('会话二正文')).toBeTruthy()
    expect(screen.queryByText('会话一正文')).toBeNull()
    expect(screen.getByRole('heading', { name: '真实研究二' })).toBeTruthy()
    fireEvent.click(sidebar().getByRole('button', { name: '新建研究' }))
    expect(screen.getByRole('heading', { name: '新建投资研究' })).toBeTruthy()
    expect(screen.queryByText('会话二正文')).toBeNull()
    expect(api.stop).toHaveBeenCalled()
  })

  it('快速切换后迟到的旧历史不能覆盖新会话', async () => {
    let finish!: (items: ReturnType<typeof history>) => void
    api.messages.mockImplementation(async (id: string) => id === 's1'
      ? new Promise<ReturnType<typeof history>>((resolve) => { finish = resolve })
      : id === 's2' ? history('新会话正文') : [])
    const { container } = render(<TooltipProvider><AiResearchView /></TooltipProvider>)
    const sidebar = () => within(container.querySelector('.desktop-sidebar') as HTMLElement)
    fireEvent.click(await sidebar().findByRole('button', { name: /真实研究一/ }))
    await waitFor(() => expect(finish).toBeTypeOf('function'))
    fireEvent.click(sidebar().getByRole('button', { name: /真实研究二/ }))
    expect(await screen.findByText('新会话正文')).toBeTruthy()
    await act(async () => { finish(history('迟到旧会话')) })
    expect(screen.queryByText('迟到旧会话')).toBeNull()
    expect(screen.getByRole('heading', { name: '真实研究二' })).toBeTruthy()
  })

  it('回车提交后当前会话标题和研究列表立即使用真实首问', async () => {
    const { container } = render(<TooltipProvider><AiResearchView /></TooltipProvider>)
    const input = screen.getByRole('textbox', { name: '研究问题' })
    fireEvent.change(input, { target: { value: '这是一条当前真实研究问题' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit' }).hasAttribute('disabled')).toBe(false))
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByRole('heading', { name: '这是一条当前真实研究问题' })).toBeTruthy()
    expect(api.submit).toHaveBeenCalledTimes(1)
    expect(container.querySelector('.desktop-sidebar .thread-item--active b')?.textContent).toBe('这是一条当前真实研究问题')
  })
})
