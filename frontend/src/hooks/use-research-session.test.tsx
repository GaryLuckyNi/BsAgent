// @vitest-environment jsdom

import type { Message } from '@langchain/langgraph-sdk'
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PersistedConversationMessage } from '../conversation-session'
import { useResearchSession } from './use-research-session'

const sdk = vi.hoisted(() => ({
  messages: [] as Message[],
  cloneMessagesOnRead: false,
  options: undefined as {
    throttle?: boolean | number
    onCustomEvent?: (event: Record<string, unknown>) => void
    onFinish?: () => void
    onError?: (error: Error) => void
  } | undefined,
  submit: vi.fn(async (_input: unknown, _options: unknown): Promise<void> => undefined),
  stop: vi.fn(async () => undefined)
}))

const runs = vi.hoisted(() => ({
  history: vi.fn(async (): Promise<PersistedConversationMessage[]> => []),
  active: vi.fn(async (): Promise<unknown> => null),
  stop: vi.fn(async () => ({ run_id: 'r1', status: 'cancelled' })),
  subscribe: vi.fn(async (_session: string, _run: string, _identity: unknown, _after: number, _signal: AbortSignal, _onEvent: (event: unknown) => void) => 0)
}))

vi.mock('../services/research-runs', () => ({
  loadActiveRun: runs.active, stopResearchRun: runs.stop, subscribeRunEvents: runs.subscribe
}))

vi.mock('@langchain/langgraph-sdk/react', () => ({
  FetchStreamTransport: class FetchStreamTransport {},
  useStream: (options: {
    throttle?: boolean | number
    onCustomEvent?: (event: Record<string, unknown>) => void
    onFinish?: () => void
    onError?: (error: Error) => void
  }) => {
    sdk.options = options
    return {
      messages: sdk.cloneMessagesOnRead ? [...sdk.messages] : sdk.messages,
      isLoading: false,
      submit: sdk.submit,
      stop: sdk.stop
    }
  }
}))

vi.mock('../services/research-api', () => ({
  agentProtocolStreamUrl: () => '/api/stream',
  agentRequestHeaders: () => ({}),
  checkAgentHealth: async () => true,
  loadSessionMessages: runs.history
}))

const pages = vi.hoisted(() => ({ load: vi.fn() }))
vi.mock('../services/research-history-pages', () => ({ loadResearchHistoryPage: pages.load }))
function persisted(sequence: number): PersistedConversationMessage {
  return { message_id: `m${sequence}`, sequence_no: sequence, role: sequence % 2 ? 'user' : 'assistant', status: 'completed', content: `消息${sequence}`, metadata: {} }
}

describe('useResearchSession', () => {
  it('来源先于正文到达时保留稳定编号，最终响应更新资料而不生成虚假引用', async () => {
    const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
    await act(async () => { await result.current.sendMessage('分析') })
    const source = { source_id: 'source_7', index: 7, url: 'https://example.com/report', title: '报告', snippet: '报告摘要' }
    act(() => sdk.options?.onCustomEvent?.({ type: 'answer_sources', sources: [source] }))
    expect(result.current.messages.at(-1)?.evidence).toEqual([{ id: 'source_7', index: 7, url: source.url, title: '报告', summary: '报告摘要' }])
    sdk.messages = [{ type: 'ai', id: 'answer', content: '结论（[7](https://example.com/report)）。' } as Message]
    rerender()
    act(() => sdk.options?.onCustomEvent?.({ type: 'final', response: { content: '结论。', sources: [{ ...source, title: '年度报告' }] } }))
    expect(result.current.messages.at(-1)?.evidence?.[0]).toMatchObject({ index: 7, title: '年度报告' })
    expect(result.current.messages.at(-1)?.citations).toBeUndefined()
  })

  it('最终响应省略来源时保留公开流已收到的资料', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
    await act(async () => { await result.current.sendMessage('分析') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'answer_sources', sources: [{ source_id: 'source_3', index: 3, url: 'https://example.com', title: '公开资料' }] }))
    act(() => sdk.options?.onCustomEvent?.({ type: 'final', response: { content: '结论。' } }))
    expect(result.current.messages.at(-1)?.evidence?.[0]).toMatchObject({ index: 3, title: '公开资料' })
  })

  it('使用服务端正文开始时刻的用时，排除整句缓冲、传输和页面更新延迟', async () => {
    const clock = vi.spyOn(Date, 'now')
    try {
      const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
      await act(async () => { await result.current.sendMessage('分析') })
      const submittedAt = Date.parse(result.current.messages.at(-1)!.createdAt)
      clock.mockReturnValue(submittedAt + 30000)
      act(() => sdk.options?.onCustomEvent?.({ type: 'answer_started', elapsed_ms: 5400 }))
      sdk.messages = [{ type: 'ai', id: 'answer', content: '结论。' } as Message]
      rerender()
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBe(5)
      clock.mockReturnValue(submittedAt + 90000)
      act(() => sdk.options?.onCustomEvent?.({ type: 'answer_started', elapsed_ms: 80000 }))
      act(() => sdk.options?.onCustomEvent?.({ type: 'final', response: { content: '完整答案。' } }))
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBe(5)
    } finally { clock.mockRestore() }
  })

  it.each([-1, NaN, Infinity, '5400', undefined])('忽略不合法的服务端计时：%s', async (elapsed) => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
    await act(async () => { await result.current.sendMessage('分析') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'answer_started', elapsed_ms: elapsed }))
    expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBeUndefined()
  })

  it('服务端计时晚于正文到达时等待真实测量，不按前端接收时间估算', async () => {
    const clock = vi.spyOn(Date, 'now')
    try {
      const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
      await act(async () => { await result.current.sendMessage('分析') })
      clock.mockReturnValue(Date.parse(result.current.messages.at(-1)!.createdAt) + 30000)
      sdk.messages = [{ type: 'ai', id: 'answer', content: '结论。' } as Message]
      rerender()
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBeUndefined()
      act(() => sdk.options?.onCustomEvent?.({ type: 'answer_started', elapsed_ms: 5400 }))
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBe(5)
    } finally { clock.mockRestore() }
  })

  it('没有服务端测量时，提交、进展、正文和 final 都不会自行生成思考秒数', async () => {
    const clock = vi.spyOn(Date, 'now')
    try {
      const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
      await act(async () => { await result.current.sendMessage('分析问题') })
      const submittedAt = Date.parse(result.current.messages.at(-1)!.createdAt)
      clock.mockReturnValue(submittedAt + 6000)
      act(() => sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'goal', id: 'goal', message: '研究目标' }))
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBeUndefined()
      clock.mockReturnValue(submittedAt + 12500)
      sdk.messages = [{ type: 'ai', id: 'answer', content: '第一句答案。' } as Message]
      rerender()
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBeUndefined()
      clock.mockReturnValue(submittedAt + 30000)
      sdk.messages = [{ type: 'ai', id: 'answer', content: '第一句答案。第二句。' } as Message]
      rerender()
      act(() => sdk.options?.onCustomEvent?.({ type: 'final', response: { content: '完整答案。' } }))
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBeUndefined()
    } finally { clock.mockRestore() }
  })

  it('只有 final 而没有计时标记时不使用总等待时间充当思考时间', async () => {
    const clock = vi.spyOn(Date, 'now')
    try {
      const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
      await act(async () => { await result.current.sendMessage('直接回答') })
      clock.mockReturnValue(Date.parse(result.current.messages.at(-1)!.createdAt) + 4500)
      act(() => sdk.options?.onCustomEvent?.({ type: 'final', response: { content: '答案。' } }))
      expect(result.current.messages.at(-1)?.thinkingDurationSeconds).toBeUndefined()
    } finally { clock.mockRestore() }
  })

  it('断线重连保留已冻结的思考时间，不从恢复组件时重新计时', async () => {
    const clock = vi.spyOn(Date, 'now')
    try {
      const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
      await act(async () => { await result.current.sendMessage('需要研究') })
      const submittedAt = Date.parse(result.current.messages.at(-1)!.createdAt)
      act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' }))
      clock.mockReturnValue(submittedAt + 5000)
      act(() => sdk.options?.onCustomEvent?.({ type: 'answer_started', elapsed_ms: 5000 }))
      sdk.messages = [{ type: 'ai', id: 'a1', content: '第一句。' } as Message]
      rerender()
      clock.mockReturnValue(submittedAt + 60000)
      runs.subscribe.mockImplementationOnce(async (_s, _r, _i, _a, _sig, onEvent) => {
        onEvent({ sequence: 1, event: 'messages', data: [{ type: 'ai', id: 'a1', content: '第一句。后续内容。' }, {}] })
        onEvent({ sequence: 2, event: 'custom', data: { type: 'run_status', run_id: 'r1', status: 'completed' } })
        return 2
      })
      await act(async () => { sdk.options?.onFinish?.(); await Promise.resolve() })
      expect(result.current.messages.at(-1)).toMatchObject({
        content: '第一句。后续内容。', thinkingDurationSeconds: 5, status: 'complete'
      })
    } finally { clock.mockRestore() }
  })

  it('指定会话 ID 时读取该会话，空历史不生成演示分析', async () => {
    const identity = { tenantId: 'tenant-1', userId: 'user-1' }
    const { result } = renderHook(() => useResearchSession(identity, 'chosen-session'))
    await act(async () => { await Promise.resolve() })
    expect(result.current.sessionId).toBe('chosen-session')
    expect(runs.history).toHaveBeenCalledWith('chosen-session', identity)
    expect(result.current.messages).toEqual([])
    await act(async () => { await result.current.sendMessage('实际首问') })
    expect(result.current.submittedMessageId).toBe(result.current.messages[0].id)
  })
  beforeEach(() => {
    const values = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
        removeItem: (key: string) => values.delete(key),
        clear: () => values.clear(),
        key: (index: number) => [...values.keys()][index] ?? null,
        get length() { return values.size }
      }
    })
    sdk.messages = []
    sdk.cloneMessagesOnRead = false
    sdk.options = undefined
    sdk.submit.mockClear()
    sdk.stop.mockClear()
    pages.load.mockReset().mockResolvedValue([])
    runs.history.mockReset().mockResolvedValue([])
    runs.active.mockReset().mockResolvedValue(null)
    runs.stop.mockClear()
    runs.subscribe.mockReset().mockResolvedValue(0)
    window.localStorage.clear()
  })

  it('初始化后立即定位已加载消息，不重置会话和订阅', async () => {
    runs.history.mockResolvedValue([persisted(201)])
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
    await act(async () => { await result.current.revealMessage('m201', 201) })
    expect(result.current.messages.map((message) => message.id)).toEqual(['m201'])
    expect(pages.load).not.toHaveBeenCalled()
    expect(runs.history).toHaveBeenCalledTimes(1)
    expect(sdk.stop).not.toHaveBeenCalled()
  })

  it.each([false, true])('刚完成的一问一答可按服务端ID定位，不补入重复历史（已有历史：%s）', async (hasHistory) => {
    const previous = hasHistory ? [persisted(1), persisted(2)] : []
    runs.history.mockResolvedValue(previous)
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
    await act(async () => { await result.current.sendMessage('新的追问') })
    act(() => sdk.options?.onCustomEvent?.({
      type: 'run_started', run_id: 'new-run', message_id: 'server-answer', user_message_id: 'server-question'
    }))
    act(() => sdk.options?.onCustomEvent?.({ type: 'final', response: { content: '新的回答' } }))
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'new-run', status: 'completed' }))

    await act(async () => {
      await result.current.revealMessage('server-answer', previous.length + 2)
      await result.current.revealMessage('server-question', previous.length + 1)
    })

    expect(result.current.messages.slice(-2)).toMatchObject([
      { id: 'server-question', role: 'user', content: '新的追问' },
      { id: 'server-answer', role: 'assistant', content: '新的回答', status: 'complete' }
    ])
    expect(result.current.messages).toHaveLength(previous.length + 2)
    expect(result.current.submittedMessageId).toBe('server-question')
    expect(pages.load).not.toHaveBeenCalled()
  })

  it('定位200条以外消息逐页补齐连续历史，同时保留最新流式消息', async () => {
    runs.history.mockResolvedValue(Array.from({ length: 200 }, (_, index) => persisted(index + 401)))
    pages.load.mockResolvedValueOnce(Array.from({ length: 200 }, (_, index) => persisted(index + 201))).mockResolvedValueOnce(Array.from({ length: 200 }, (_, index) => persisted(index + 1)))
    const identity = { tenantId: 't', userId: 'u' }
    const { result, rerender } = renderHook(() => useResearchSession(identity, 's'))
    await act(async () => { await result.current.sendMessage('最新追问') })
    sdk.messages = [{ type: 'ai', id: 'new-stream', content: '实时新增内容' } as Message]
    rerender()
    const latestId = result.current.messages.at(-1)?.id
    await act(async () => { await result.current.revealMessage('m100', 100) })
    expect(pages.load.mock.calls.map((call) => call[2])).toEqual([401, 201])
    expect(result.current.messages.slice(0, 600).map((message) => message.id)).toEqual(Array.from({ length: 600 }, (_, index) => `m${index + 1}`))
    expect(result.current.messages.at(-1)).toMatchObject({ id: latestId, content: '实时新增内容', status: 'streaming' })
    expect(sdk.stop).not.toHaveBeenCalled()
  })

  it('分页加载期间的流式更新与最新终态不被历史快照覆盖', async () => {
    runs.history.mockResolvedValue([persisted(401)])
    let resolvePage!: (items: PersistedConversationMessage[]) => void
    pages.load.mockImplementationOnce(() => new Promise((resolve) => { resolvePage = resolve }))
    const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
    await act(async () => { await result.current.sendMessage('追问') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'live', message_id: 'answer' }))
    let revealing!: Promise<void>
    act(() => { revealing = result.current.revealMessage('m1', 1) })
    await act(async () => { await Promise.resolve() })
    sdk.messages = [{ type: 'ai', id: 'answer', content: '分页期间的新回答' } as Message]
    rerender()
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'live', status: 'cancelled' }))
    await act(async () => { resolvePage([persisted(1), persisted(401)]); await revealing })
    expect(result.current.messages.at(-1)).toMatchObject({ content: '分页期间的新回答', status: 'cancelled' })
    expect(result.current.messages.filter((message) => message.id === 'm401')).toHaveLength(1)
    expect(sdk.stop).not.toHaveBeenCalled()
    expect(runs.active).toHaveBeenCalledTimes(1)
  })

  it.each(['取消', '会话切换', '身份切换', '新一轮提交'])('分页响应迟到时%s不会写回旧历史', async (change) => {
    runs.history.mockResolvedValue([persisted(401)])
    let resolvePage!: (items: PersistedConversationMessage[]) => void
    pages.load.mockImplementationOnce(() => new Promise((resolve) => { resolvePage = resolve }))
    const controller = new AbortController()
    const { result, rerender } = renderHook(({ id, user }) => useResearchSession({ tenantId: 't', userId: user }, id), { initialProps: { id: 's', user: 'u' } })
    await act(async () => { await Promise.resolve() })
    let revealing!: Promise<void>
    act(() => { revealing = result.current.revealMessage('m1', 1, controller.signal) })
    await act(async () => { await Promise.resolve() })
    if (change === '取消') controller.abort()
    if (change === '会话切换') rerender({ id: 'other', user: 'u' })
    if (change === '身份切换') rerender({ id: 's', user: 'other' })
    if (change === '新一轮提交') await act(async () => { await result.current.sendMessage('新的追问') })
    await act(async () => {
      resolvePage([persisted(1)])
      await expect(revealing).rejects.toMatchObject({ name: 'AbortError' })
    })
    expect(result.current.messages.some((message) => message.id === 'm1')).toBe(false)
  })

  it('找不到消息或分页游标不前进时返回中文错误', async () => {
    runs.history.mockResolvedValue([persisted(401)])
    pages.load.mockResolvedValue([persisted(401)])
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }, 's'))
    await act(async () => { await expect(result.current.revealMessage('missing', 1)).rejects.toThrow('未找到目标历史消息') })
    expect(pages.load).toHaveBeenCalledTimes(1)
  })

  it('刷新后订阅原 run，从头重放正文而不重复快照', async () => {
    runs.active.mockResolvedValue({ run_id: 'r1', message_id: 'a1', status: 'running', content: '已有部分', last_event_sequence: 1 })
    runs.subscribe.mockImplementationOnce(async (_s, _r, _i, _a, _sig, onEvent) => {
      onEvent({ sequence: 1, event: 'messages', data: [{ type: 'ai', id: 'a1', content: '已有部分' }, {}] })
      onEvent({ sequence: 2, event: 'messages', data: [{ type: 'ai', id: 'a1', content: '继续生成' }, {}] })
      onEvent({ sequence: 3, event: 'custom', data: { type: 'run_status', status: 'completed' } })
      return 3
    })
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await Promise.resolve() })
    expect(result.current.messages.at(-1)).toMatchObject({ id: 'a1', content: '已有部分继续生成', status: 'complete' })
    expect(sdk.submit).not.toHaveBeenCalled()
  })

  it('点击停止请求后端取消，保留部分回答且不误标成功', async () => {
    const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('问题') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' }))
    sdk.messages = [{ type: 'ai', id: 'a1', content: '已生成部分' } as Message]
    rerender()
    await act(async () => { await result.current.cancelMessage() })
    act(() => sdk.options?.onFinish?.())
    expect(runs.stop).toHaveBeenCalledWith(expect.any(String), 'r1', expect.objectContaining({ userId: 'user-1' }))
    expect(result.current.messages.at(-1)).toMatchObject({ content: '已生成部分', status: 'cancelled' })
  })

  it('停止确认前保持忙状态，收到取消终态后才允许新一轮', async () => {
    runs.stop.mockResolvedValueOnce({ run_id: 'r1', status: 'stop_requested' })
    const { result, rerender } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('第一问') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' }))
    sdk.messages = [{ type: 'ai', id: 'a1', content: '部分回答' } as Message]
    rerender()
    await act(async () => { await result.current.cancelMessage() })
    expect(result.current.isStreaming).toBe(true)
    await act(async () => { await result.current.sendMessage('不能抢先执行') })
    expect(sdk.submit).toHaveBeenCalledTimes(1)
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'r1', status: 'cancelled' }))
    expect(result.current.isStreaming).toBe(false)
    await act(async () => { await result.current.sendMessage('第二问') })
    expect(sdk.submit).toHaveBeenCalledTimes(2)
    expect(result.current.messages.at(-3)).toMatchObject({ content: '部分回答', status: 'cancelled' })
  })

  it('旧停止请求迟到返回不会断开已经开始的新一轮', async () => {
    let resolveStop!: (value: { run_id: string; status: string }) => void
    runs.stop.mockImplementationOnce(() => new Promise((resolve) => { resolveStop = resolve }))
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('第一问') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' }))
    let pending!: Promise<void>
    act(() => { pending = result.current.cancelMessage() })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'r1', status: 'cancelled' }))
    await act(async () => { await result.current.sendMessage('第二问') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r2', message_id: 'a2' }))
    sdk.stop.mockClear()
    await act(async () => { resolveStop({ run_id: 'r1', status: 'cancelled' }); await pending })
    expect(sdk.stop).not.toHaveBeenCalled()
    expect(result.current.messages.at(-1)?.status).toBe('streaming')
  })

  it('迟到的停止确认不能把 SSE 已确认的取消降级为正在停止', async () => {
    let resolveStop!: (value: { run_id: string; status: string }) => void
    runs.stop.mockImplementationOnce(() => new Promise((resolve) => { resolveStop = resolve }))
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('问题') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' }))
    let pending!: Promise<void>
    act(() => { pending = result.current.cancelMessage() })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'r1', status: 'cancelled' }))
    await act(async () => { resolveStop({ run_id: 'r1', status: 'stop_requested' }); await pending })
    expect(result.current.messages.at(-1)?.status).toBe('cancelled')
    expect(result.current.isStreaming).toBe(false)
    await act(async () => { await result.current.sendMessage('下一问') })
    expect(sdk.submit).toHaveBeenCalledTimes(2)
  })

  it.each(['completed', 'cancelled'] as const)('历史快照之后任务已%s且 active 为空时仍补齐原回答', async (status) => {
    runs.history.mockResolvedValueOnce([
      { message_id: 'u1', role: 'user', status: 'completed', content: '问题', sequence_no: 1, metadata: {} },
      { message_id: 'a1', run_id: 'r1', role: 'assistant', status: 'streaming', content: '部分', sequence_no: 2, metadata: {} }
    ])
    runs.subscribe.mockImplementationOnce(async (_s, _r, _i, _a, _sig, onEvent) => {
      onEvent({ sequence: 1, event: 'messages', data: [{ type: 'ai', id: 'a1', content: '部分回答已补齐' }, {}] })
      onEvent({ sequence: 2, event: 'custom', data: { type: 'run_status', run_id: 'r1', status } })
      return 2
    })
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await Promise.resolve() })
    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages.at(-1)).toMatchObject({ id: 'a1', content: '部分回答已补齐', status: status === 'completed' ? 'complete' : 'cancelled' })
    expect(result.current.isStreaming).toBe(false)
    expect(sdk.submit).not.toHaveBeenCalled()
  })

  it('历史补订阅跨过已结束的中断阶段后只保留恢复阶段的部分回答', async () => {
    runs.history.mockResolvedValueOnce([
      { message_id: 'a1', run_id: 'r1', role: 'assistant', status: 'streaming', content: '新阶段', sequence_no: 1, metadata: {} }
    ])
    runs.subscribe.mockImplementationOnce(async (_s, _r, _i, _a, _sig, onEvent) => {
      onEvent({ sequence: 1, event: 'custom', data: { type: 'run_started', run_id: 'r1', message_id: 'a1', replay_after: 0 } })
      onEvent({ sequence: 2, event: 'messages', data: [{ type: 'ai', id: 'a1', content: '旧阶段的正文' }, {}] })
      onEvent({ sequence: 3, event: 'custom', data: { type: 'run_status', run_id: 'r1', status: 'interrupted' } })
      onEvent({ sequence: 4, event: 'custom', data: { type: 'run_started', run_id: 'r1', message_id: 'a1', replay_after: 3 } })
      onEvent({ sequence: 5, event: 'messages', data: [{ type: 'ai', id: 'a1', content: '新阶段的部分回答' }, {}] })
      onEvent({ sequence: 6, event: 'custom', data: { type: 'run_status', run_id: 'r1', status: 'cancelled' } })
      return 6
    })
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await Promise.resolve() })
    expect(result.current.messages.at(-1)).toMatchObject({ id: 'a1', content: '新阶段的部分回答', status: 'cancelled' })
  })

  it('恢复查询尚未返回时拒绝发送新问题', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('第一问') })
    let resolveActive!: (value: unknown) => void
    runs.active.mockImplementationOnce(() => new Promise((resolve) => { resolveActive = resolve }))
    act(() => sdk.options?.onFinish?.())
    expect(result.current.isStreaming).toBe(true)
    await act(async () => { await result.current.sendMessage('不能抢先发送') })
    expect(sdk.submit).toHaveBeenCalledTimes(1)
    await act(async () => { resolveActive(null); await Promise.resolve() })
  })

  it('accepted 事件送达前断线也查询已受理任务，不把空回答标为成功', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('问题') })
    runs.active.mockResolvedValueOnce({ run_id: 'r1', message_id: 'a1', status: 'running', content: '已保存', last_event_sequence: 2 })
    runs.subscribe.mockImplementationOnce(async (_s, _r, _i, _a, _sig, onEvent) => {
      onEvent({ sequence: 2, event: 'messages', data: [{ type: 'ai', id: 'a1', content: '已保存' }, {}] })
      onEvent({ sequence: 3, event: 'custom', data: { type: 'run_status', run_id: 'r1', status: 'completed' } })
      return 3
    })
    await act(async () => { sdk.options?.onFinish?.(); await Promise.resolve() })
    expect(runs.subscribe).toHaveBeenCalled()
    expect(result.current.messages.at(-1)).toMatchObject({ content: '已保存', status: 'complete' })
  })

  it('没有受理记录也没有成功终态的断流不能显示研究完成', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('问题') })
    await act(async () => { sdk.options?.onFinish?.(); await Promise.resolve() })
    expect(result.current.messages.at(-1)?.status).toBe('error')
  })

  it('停止 HTTP 失败不能覆盖 SSE 已确认的取消终态', async () => {
    let rejectStop!: (reason: Error) => void
    runs.stop.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectStop = reject }))
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('问题') })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' }))
    let pending!: Promise<void>
    act(() => { pending = result.current.cancelMessage() })
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'r1', status: 'cancelled' }))
    await act(async () => { rejectStop(new Error('网络中断')); await pending })
    expect(result.current.messages.at(-1)?.status).toBe('cancelled')
  })

  it('final 之后丢失终态时仍恢复查询，确认完成后可继续提问', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('第一问') })
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' })
      sdk.options?.onCustomEvent?.({ type: 'final', run_id: 'r1', response: { content: '完整回答' } })
    })
    runs.subscribe.mockImplementationOnce(async (_s, _r, _i, _a, _sig, onEvent) => {
      onEvent({ sequence: 1, event: 'custom', data: { type: 'final', run_id: 'r1', response: { content: '完整回答' } } })
      onEvent({ sequence: 2, event: 'custom', data: { type: 'run_status', run_id: 'r1', status: 'completed' } })
      return 2
    })
    await act(async () => { sdk.options?.onFinish?.(); await Promise.resolve() })
    await act(async () => { await result.current.sendMessage('第二问') })
    expect(sdk.submit).toHaveBeenCalledTimes(2)
  })

  it('终态之后的传输错误不把取消改成失败', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('问题') })
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1', message_id: 'a1' })
      sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'r1', status: 'cancelled' })
      sdk.options?.onError?.(new Error('流断开'))
    })
    expect(result.current.messages.at(-1)?.status).toBe('cancelled')
  })

  it('新一轮答案开始前不会复用上一轮 AI 消息', async () => {
    sdk.messages = [{
      type: 'ai',
      id: 'previous-assistant',
      content: '上一轮的错误答案'
    } as Message]
    const { result, rerender } = renderHook(() => useResearchSession({
      tenantId: 'tenant-1',
      userId: 'user-1'
    }))

    await act(async () => {
      await result.current.sendMessage('什么是市盈率？')
    })

    const submitOptions = sdk.submit.mock.calls[0][1] as {
      optimisticValues: { messages: Message[] }
    }
    sdk.messages = [...submitOptions.optimisticValues.messages]
    rerender()

    const currentAssistant = result.current.messages.at(-1)
    expect(currentAssistant?.role).toBe('assistant')
    expect(currentAssistant?.content).toBe('')
    expect(currentAssistant?.status).toBe('streaming')
  })

  it('启用 SDK 原生批处理以合并同一事件循环的 token 更新', () => {
    renderHook(() => useResearchSession({
      tenantId: 'tenant-1',
      userId: 'user-1'
    }))

    expect(sdk.options?.throttle).toBe(true)
  })

  it('相同的 SDK 答案快照不会重复写入本地消息', async () => {
    sdk.cloneMessagesOnRead = true
    const { result, rerender } = renderHook(() => useResearchSession({
      tenantId: 'tenant-1',
      userId: 'user-1'
    }))

    await act(async () => {
      await result.current.sendMessage('解释净息差。')
    })

    sdk.messages = [{
      type: 'ai',
      id: 'streamed-assistant',
      content: '净息差会影响银行的利息净收入。'
    } as Message]

    expect(() => rerender()).not.toThrow()
    expect(result.current.messages.at(-1)?.content)
      .toBe('净息差会影响银行的利息净收入。')
  })

  it('截断重试后的 final 完整正文不会被旧的流式快照覆盖', async () => {
    const { result, rerender } = renderHook(() => useResearchSession({
      tenantId: 'tenant-1',
      userId: 'user-1'
    }))
    await act(async () => {
      await result.current.sendMessage('分析连续涨停原因。')
    })

    act(() => {
      sdk.options?.onCustomEvent?.({
        type: 'final',
        response: { content: '这是重试后生成的完整回答。' }
      })
    })
    sdk.messages = [{
      type: 'ai',
      id: 'partial-stream-answer',
      content: '这是被 token 上限截断的半截'
    } as Message]
    rerender()

    expect(result.current.messages.at(-1)?.content).toBe('这是重试后生成的完整回答。')
  })

  it('忽略遗留 thinking_delta，不把内部推理写入用户消息', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('测试推理流。') })
    act(() => {
      sdk.options?.onCustomEvent?.({
        type: 'thinking_delta',
        id: 'planner:reasoning',
        content: '检查 allowed_taxonomy、skill_name 和 required_schema。'
      })
    })
    expect(result.current.messages.at(-1)?.reasoning).toBeUndefined()
  })

  it('接收目标与计划事件，工具状态更新不覆盖计划列表', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('分析当日市场') })
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'goal', id: 'goal', label: '研究目标', message: '分析当日市场', status: 'completed' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'plan', id: 'plan', label: '研究计划', message: '1. 获取行情\n2. 核验消息', status: 'completed' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'task', message: '正在获取行情', status: 'started' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'task', message: '已取得行情', status: 'completed' })
    })
    const progress = result.current.messages.at(-1)!.progress!
    expect(progress.map((item) => item.kind)).toEqual(['goal', 'plan', 'activity'])
    expect(progress[1].message).toBe('1. 获取行情\n2. 核验消息')
    expect(progress[2].status).toBe('completed')
    expect(progress.map((item) => item.sequence)).toEqual([0, 1, 2])
  })

  it('研究目标和计划的流式快照即时覆盖原条目，完成后保留全部文字', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }))
    await act(async () => { await result.current.sendMessage('核实财报并分析') })
    const publish = (kind: 'goal' | 'plan', message: string, status = 'started') => {
      act(() => sdk.options?.onCustomEvent?.({
        type: 'progress', kind, id: kind, label: kind === 'goal' ? '研究目标' : '研究计划', message, status
      }))
    }
    publish('goal', '核实财报')
    expect(result.current.messages.at(-1)?.progress?.[0].message).toBe('核实财报')
    publish('goal', '核实财报并分析增长原因')
    expect(result.current.messages.at(-1)?.progress).toHaveLength(1)
    expect(result.current.messages.at(-1)?.progress?.[0].message).toBe('核实财报并分析增长原因')
    publish('plan', '1. 取得原始财报')
    const goal = '核实不同期间的财报口径并完成推演与验证。'.repeat(40) + '目标结尾。'
    const plan = `1. ${'核实必要字段和期间。'.repeat(45)}第一项结尾。\n2. 核对直接披露值。`
    publish('goal', goal, 'completed')
    publish('plan', plan, 'completed')
    const progress = result.current.messages.at(-1)!.progress!
    expect(progress.map((item) => [item.message, item.status, item.sequence])).toEqual([
      [goal, 'completed', 0], [plan, 'completed', 2]
    ])
  })

  it('重规划覆盖原失败活动，最终未补偿时仍可更新为告警', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }))
    await act(async () => { await result.current.sendMessage('使用流程查询') })
    const publish = (status: string, message: string) => act(() => sdk.options?.onCustomEvent?.({
      type: 'progress', kind: 'activity', id: 'skill_agent:report', stage: 'skill_agent', status, message
    }))
    publish('pending', '正在评估后续方案')
    expect(result.current.messages.at(-1)?.progress?.[0].status).toBe('pending')
    publish('superseded', '已调整研究流程')
    expect(result.current.messages.at(-1)?.progress).toEqual([
      expect.objectContaining({ status: 'superseded', message: '已调整研究流程', sequence: 0 })
    ])
    publish('failed', '研究流程未完成')
    expect(result.current.messages.at(-1)?.progress?.[0]).toMatchObject({ status: 'failed', sequence: 0 })
  })

  it('同组成功任务不会吞掉最终未补偿失败的告警，替代后恢复中性状态', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 't', userId: 'u' }))
    await act(async () => { await result.current.sendMessage('并行查询') })
    const publish = (taskId: string, status: string) => act(() => sdk.options?.onCustomEvent?.({
      type: 'progress', kind: 'activity', id: `tool_agent:${taskId}`, stage: 'tool_agent',
      activity_type: 'market_data', group_id: 'market_data:1', task_id: taskId,
      status, message: status === 'superseded' ? '已调整研究流程' : '查询状态'
    }))
    publish('a', 'completed')
    publish('b', 'pending')
    expect(result.current.messages.at(-1)?.progress?.[0].status).toBe('pending')
    publish('b', 'failed')
    expect(result.current.messages.at(-1)?.progress?.[0].status).toBe('failed')
    publish('b', 'superseded')
    expect(result.current.messages.at(-1)?.progress?.[0].status).toBe('completed')
    expect(result.current.messages.at(-1)?.progress).toHaveLength(1)
  })

  it('兼容旧研究摘要与工具活动的到达顺序，工具完成不改变位置', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('测试活动顺序。') })
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'summary', id: 'plan', message: '先搜索。', status: 'completed' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'tool:1', stage: 'tool_agent', message: '正在联网检索', status: 'started' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'summary', id: 'analysis', message: '再分析。', status: 'completed' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'tool:1', stage: 'tool_agent', message: '已完成联网检索', status: 'completed' })
    })
    const message = result.current.messages.at(-1)!
    expect(message.progress?.map((item) => item.message)).toEqual([
      '先搜索。',
      '已完成联网检索',
      '再分析。'
    ])
    expect(message.progress?.map((item) => item.sequence)).toEqual([0, 1, 2])
  })

  it('忽略交错的原始思考并保留公开工具活动顺序', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('测试交错流。') })
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'thinking_delta', id: 'planner:reasoning', content: '先搜索。' })
      sdk.options?.onCustomEvent?.({
        type: 'progress', kind: 'activity', id: 'tool:1', stage: 'tool_agent',
        message: '正在联网检索', status: 'started'
      })
      sdk.options?.onCustomEvent?.({ type: 'thinking_delta', id: 'analysis:reasoning', content: '再分析。' })
    })

    const message = result.current.messages.at(-1)!
    expect(message.reasoning).toBeUndefined()
    expect(message.progress?.map((item) => item.sequence)).toEqual([0])
  })

  it('阶段状态只更新标题，不进入研究过程时间线', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('测试状态。') })
    act(() => {
      sdk.options?.onCustomEvent?.({
        type: 'progress', kind: 'status', id: 'planner', message: '正在制定研究计划。', status: 'started'
      })
    })
    const message = result.current.messages.at(-1)!
    expect(message.processStatus).toBe('正在制定研究计划。')
    expect(message.progress).toEqual([])
  })

  it('将同一轮并行搜索合并为一个真实网页统计条目', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('测试搜索聚合。') })
    act(() => {
      sdk.options?.onCustomEvent?.({
        type: 'progress', kind: 'activity', id: 'tool:a', stage: 'tool_agent',
        message: '正在联网检索：搜索量价', status: 'started',
        activity_type: 'web_search', group_id: 'web_search:1', task_id: 'a',
        query: '搜索量价', result_count: 0, result_urls: []
      })
      sdk.options?.onCustomEvent?.({
        type: 'progress', kind: 'activity', id: 'tool:b', stage: 'tool_agent',
        message: '正在联网检索：搜索资金', status: 'started',
        activity_type: 'web_search', group_id: 'web_search:1', task_id: 'b',
        query: '搜索资金', result_count: 0, result_urls: []
      })
      sdk.options?.onCustomEvent?.({
        type: 'progress', kind: 'activity', id: 'tool:a', stage: 'tool_agent',
        message: '已完成联网检索：搜索量价', status: 'completed',
        activity_type: 'web_search', group_id: 'web_search:1', task_id: 'a',
        query: '搜索量价', result_count: 2,
        result_urls: ['https://a.example/x', 'https://b.example/y'], answer: '量价摘要'
      })
    })
    expect(result.current.messages.at(-1)?.progress?.[0]).toMatchObject({
      status: 'started', queries: [expect.objectContaining({ answer: '量价摘要' }), expect.objectContaining({ status: 'started' })]
    })
    act(() => {
      sdk.options?.onCustomEvent?.({
        type: 'progress', kind: 'activity', id: 'tool:b', stage: 'tool_agent',
        message: '已完成联网检索：搜索资金', status: 'completed',
        activity_type: 'web_search', group_id: 'web_search:1', task_id: 'b',
        query: '搜索资金', result_count: 2,
        result_urls: ['https://b.example/y', 'https://c.example/z'], answer: '资金摘要'
      })
    })

    expect(result.current.messages.at(-1)?.progress).toEqual([
      expect.objectContaining({
        id: 'web_search:1',
        activityType: 'web_search',
        queryCount: 2,
        resultCount: 3,
        status: 'completed',
        queries: [expect.objectContaining({ answer: '量价摘要' }), expect.objectContaining({ answer: '资金摘要' })]
      })
    ])
  })

  it('真实工具确定后迁移同一任务，不遗留通用取证的进行中状态', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('查询') })
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'tool:a', message: '取证中',
        status: 'started', activity_type: 'evidence', group_id: 'evidence:1', task_id: 'a' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'tool:b', message: '取证中',
        status: 'started', activity_type: 'evidence', group_id: 'evidence:1', task_id: 'b' })
      sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'tool:a', message: '搜索完成',
        status: 'completed', activity_type: 'web_search', group_id: 'web_search:1', task_id: 'a', answer: '实际摘要' })
    })
    expect(result.current.messages.at(-1)?.progress).toEqual([
      expect.objectContaining({ activityType: 'evidence', queryCount: 1, status: 'started',
        queries: [expect.objectContaining({ taskId: 'b' })] }),
      expect.objectContaining({ activityType: 'web_search', queryCount: 1, status: 'completed',
        queries: [expect.objectContaining({ taskId: 'a', answer: '实际摘要' })] })
    ])
    act(() => sdk.options?.onCustomEvent?.({ type: 'progress', kind: 'activity', id: 'tool:b', message: '搜索完成',
      status: 'completed', activity_type: 'web_search', group_id: 'web_search:1', task_id: 'b', answer: '第二摘要' }))
    expect(result.current.messages.at(-1)?.progress).toHaveLength(1)
    expect(result.current.messages.at(-1)?.progress?.[0].queries).toHaveLength(2)
  })

  it('恢复历史搜索摘要并保留不同任务的结果', async () => {
    runs.history.mockResolvedValueOnce([{
      message_id: 'a1', role: 'assistant', status: 'completed', content: '最终结果', sequence_no: 2,
      metadata: { research_process: ['a', 'b'].map((taskId, sequence) => ({
        id: `tool:${taskId}`, kind: 'activity', label: '获取证据', message: '已完成搜索', status: 'completed',
        sequence, activity_type: 'web_search', group_id: 'web_search:1', task_id: taskId, answer: `${taskId}摘要`
      })) }
    }])
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await Promise.resolve() })
    expect(result.current.messages.at(-1)?.progress?.[0].queries).toEqual([
      expect.objectContaining({ taskId: 'a', answer: 'a摘要' }), expect.objectContaining({ taskId: 'b', answer: 'b摘要' })
    ])
  })

  it('按语义分组同一轮市场数据工具活动', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('测试市场数据聚合。') })
    act(() => {
      for (const [taskId, label] of [['price', '查询当日行情'], ['flow', '查询资金流向']]) {
        sdk.options?.onCustomEvent?.({
          type: 'progress', kind: 'activity', id: `tool:${taskId}`, stage: 'tool_agent',
          message: `已取得市场数据：${label}`, status: 'completed',
          activity_type: 'market_data' as never, group_id: 'market_data:1',
          task_id: taskId, task_label: label
        })
      }
    })

    expect(result.current.messages.at(-1)?.progress).toEqual([
      expect.objectContaining({
        id: 'market_data:1',
        activityType: 'market_data',
        queryCount: 2,
        status: 'completed'
      })
    ])
  })

  it.each([false, true])('保存选择中断并用原助手消息提交恢复命令（批次=%s）', async (batch) => {
    const { result } = renderHook(() => useResearchSession({
      tenantId: 'tenant-1',
      userId: 'user-1'
    }))
    await act(async () => {
      await result.current.sendMessage('分析长鑫存储。')
    })
    const assistantId = result.current.messages.at(-1)!.id

    act(() => {
      sdk.options?.onCustomEvent?.({
        type: 'input_required',
        interrupt_id: 'interrupt-1',
        interaction_id: 'interaction-1',
        input_type: 'selection',
        question: '请选择证券标的',
        ...(batch ? { recovery_batch: recoveryBatch() } : {}),
        options: batch ? [
          { value: 'web_search', label: '对以上任务使用联网搜索' },
          { value: 'use_existing', label: '仅使用已有资料' }
        ] : [
          { value: '688825.SH', label: 'C长鑫', description: '证券代码：688825.SH' },
          { value: '603986.SH', label: '兆易创新', description: '证券代码：603986.SH' }
        ]
      })
      sdk.options?.onFinish?.()
    })

    expect(result.current.messages.at(-1)).toEqual(expect.objectContaining({
      id: assistantId,
      status: 'interrupted',
      userInput: expect.objectContaining({
        interactionId: 'interaction-1',
        question: '请选择证券标的'
      })
    }))

    await act(async () => {
      await result.current.resumeUserInput(assistantId, batch ? 'web_search' : '688825.SH')
    })

    expect(sdk.submit).toHaveBeenLastCalledWith(null, {
      command: {
        resume: {
          interrupt_id: 'interrupt-1',
          interaction_id: 'interaction-1',
          input_type: 'selection',
          value: batch ? 'web_search' : '688825.SH',
          ...(batch ? { presentation_version: 'permission-recovery-batch-v1', batch_digest: 'a'.repeat(64) } : {})
        }
      }
    })
  })

  it.each(['web_search', 'use_existing'])('点击%s立即关闭确认框，不等待恢复请求结束', async (value) => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('查询资料') })
    const id = result.current.messages.at(-1)!.id
    act(() => sdk.options?.onCustomEvent?.({
      type: 'input_required', interrupt_id: 'interrupt', interaction_id: 'interaction',
      input_type: 'selection', question: '是否替代取证？', recovery_batch: recoveryBatch(),
      options: [{ value: 'web_search', label: '搜索' }, { value: 'use_existing', label: '已有资料' }]
    }))
    let finish!: () => void
    sdk.submit.mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve }))
    let pending!: Promise<void>
    const submits = sdk.submit.mock.calls.length
    act(() => {
      pending = result.current.resumeUserInput(id, value)
      void result.current.resumeUserInput(id, value)
    })
    expect(sdk.submit).toHaveBeenCalledTimes(submits + 1)
    expect(result.current.messages.at(-1)?.userInput).toBeUndefined()
    expect(result.current.messages.at(-1)?.status).toBe('streaming')
    await act(async () => { finish(); await pending })
    expect(result.current.messages.at(-1)?.userInput).toBeUndefined()
  })

  it.each(['callback', 'reject'])('确认请求失败恢复原选择框并允许重试：%s', async (failure) => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('查询资料') })
    const id = result.current.messages.at(-1)!.id
    act(() => sdk.options?.onCustomEvent?.({
      type: 'input_required', interrupt_id: 'interrupt', interaction_id: 'interaction',
      input_type: 'selection', question: '是否替代取证？', recovery_batch: recoveryBatch(),
      options: [{ value: 'web_search', label: '搜索' }, { value: 'use_existing', label: '已有资料' }]
    }))
    sdk.submit.mockImplementationOnce(async () => {
      if (failure === 'reject') throw new Error('提交失败')
      sdk.options?.onError?.(new Error('提交失败'))
    })
    await act(async () => { await result.current.resumeUserInput(id, 'web_search') })
    expect(result.current.error).toBe('提交失败')
    expect(result.current.messages.at(-1)?.status).toBe('interrupted')
    expect(result.current.messages.at(-1)?.userInput?.interactionId).toBe('interaction')
    expect(result.current.messages.at(-1)?.userInput?.isSubmitting).not.toBe(true)
    const submitted = sdk.submit.mock.calls.length
    await act(async () => { await result.current.resumeUserInput(id, 'use_existing') })
    expect(sdk.submit).toHaveBeenCalledTimes(submitted + 1)
  })

  it.each(['failed', 'cancelled'])('运行%s后清除失效选择，允许下一条query', async (status) => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('查询资料') })
    const id = result.current.messages.at(-1)!.id
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1' })
      sdk.options?.onCustomEvent?.({
        type: 'input_required', interrupt_id: 'interrupt', interaction_id: 'interaction',
        input_type: 'selection', question: '是否替代取证？', recovery_batch: recoveryBatch(),
        options: [{ value: 'web_search', label: '搜索' }, { value: 'use_existing', label: '已有资料' }]
      })
    })
    expect(result.current.messages.at(-1)?.userInput).toBeDefined()
    act(() => sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'r1', status }))
    expect(result.current.messages.at(-1)?.userInput).toBeUndefined()
    const submitted = sdk.submit.mock.calls.length
    await act(async () => { await result.current.resumeUserInput(id, 'web_search') })
    expect(sdk.submit).toHaveBeenCalledTimes(submitted)
    await act(async () => { await result.current.sendMessage('下一问') })
    expect(sdk.submit).toHaveBeenCalledTimes(submitted + 1)
  })

  it('目录失效终态后的提交异常不能重新打开旧选择框', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('查询资料') })
    const id = result.current.messages.at(-1)!.id
    act(() => {
      sdk.options?.onCustomEvent?.({ type: 'run_started', run_id: 'r1' })
      sdk.options?.onCustomEvent?.({
        type: 'input_required', interrupt_id: 'interrupt', interaction_id: 'interaction',
        input_type: 'selection', question: '是否替代取证？',
        options: [{ value: 'web_search', label: '搜索' }, { value: 'use_existing', label: '已有资料' }]
      })
    })
    sdk.submit.mockImplementationOnce(async () => {
      sdk.options?.onCustomEvent?.({ type: 'run_status', run_id: 'r1', status: 'failed' })
      throw new Error('工具目录已变化，不能继续用户输入恢复。')
    })
    await act(async () => { await result.current.resumeUserInput(id, 'web_search') })
    expect(result.current.messages.at(-1)?.status).toBe('error')
    expect(result.current.messages.at(-1)?.userInput).toBeUndefined()
    const submitted = sdk.submit.mock.calls.length
    await act(async () => { await result.current.sendMessage('下一问') })
    expect(sdk.submit).toHaveBeenCalledTimes(submitted + 1)
  })

  it('恢复过程中新的中断不会被旧提交收尾关闭', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('查询资料') })
    const id = result.current.messages.at(-1)!.id
    const event = {
      type: 'input_required', interrupt_id: 'interrupt', interaction_id: 'interaction',
      input_type: 'selection', question: '是否替代取证？', recovery_batch: recoveryBatch(),
      options: [{ value: 'web_search', label: '搜索' }, { value: 'use_existing', label: '已有资料' }]
    }
    act(() => sdk.options?.onCustomEvent?.(event))
    sdk.submit.mockImplementationOnce(async () => {
      sdk.options?.onCustomEvent?.({ ...event, interaction_id: 'next-interaction', interrupt_id: 'next-interrupt' })
    })
    await act(async () => { await result.current.resumeUserInput(id, 'web_search') })
    expect(result.current.messages.at(-1)?.userInput?.interactionId).toBe('next-interaction')
  })

  it.each([
    { ...recoveryBatch(), version: 'future' },
    { ...recoveryBatch(), digest: 'invalid' },
    { ...recoveryBatch(), items: [] },
    { ...recoveryBatch(), items: [recoveryBatch().items[0], recoveryBatch().items[0]] },
    { ...recoveryBatch(), items: [{ item_id: 'incomplete' }] }
  ])('不将未知或残缺批次降级成普通选择', async (batch) => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('查询资料') })
    const id = result.current.messages.at(-1)!.id
    const submits = sdk.submit.mock.calls.length
    act(() => {
      sdk.options?.onCustomEvent?.({
        type: 'input_required', interrupt_id: 'interrupt', interaction_id: 'interaction',
        input_type: 'selection', question: '是否替代取证？', recovery_batch: batch,
        options: [{ value: 'web_search', label: '搜索' }, { value: 'use_existing', label: '已有资料' }]
      })
    })
    expect(result.current.messages.at(-1)?.userInput?.presentationError).toBeTruthy()
    await act(async () => { await result.current.resumeUserInput(id, 'web_search') })
    expect(sdk.submit).toHaveBeenCalledTimes(submits)
  })

  it('刷新后重放批次清单，恢复提交保留原摘要', async () => {
    runs.active.mockResolvedValue({ run_id: 'r1', message_id: 'a1', status: 'interrupted', content: '', last_event_sequence: 1 })
    runs.subscribe.mockImplementationOnce(async (_s, _r, _i, _a, _sig, onEvent) => {
      onEvent({ sequence: 1, event: 'custom', data: {
        type: 'input_required', interrupt_id: 'interrupt', interaction_id: 'interaction',
        input_type: 'selection', question: '是否替代取证？', recovery_batch: recoveryBatch(),
        options: [{ value: 'web_search', label: '对以上任务使用联网搜索' }, { value: 'use_existing', label: '仅使用已有资料' }]
      } })
      onEvent({ sequence: 2, event: 'custom', data: { type: 'run_status', run_id: 'r1', status: 'interrupted' } })
      return 2
    })
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await Promise.resolve() })
    expect(result.current.messages.at(-1)?.userInput?.recoveryBatch).toEqual(recoveryBatch())
    await act(async () => { await result.current.resumeUserInput('a1', 'use_existing') })
    expect(sdk.submit).toHaveBeenLastCalledWith(null, { command: { resume: {
      interrupt_id: 'interrupt', interaction_id: 'interaction', input_type: 'selection', value: 'use_existing',
      presentation_version: 'permission-recovery-batch-v1', batch_digest: 'a'.repeat(64)
    } } })
  })
})

function recoveryBatch() {
  return {
    version: 'permission-recovery-batch-v1', batch_id: 'batch', digest: 'a'.repeat(64),
    items: [{ item_id: 'c1', task_goal: '研究目标', original_tool: 'mcp.x.api',
      reason: '账号权限不足', source_notice: '不等价于原接口数据' }]
  }
}
