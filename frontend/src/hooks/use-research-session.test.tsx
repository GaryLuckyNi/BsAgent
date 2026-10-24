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
  submit: vi.fn(async (_input: unknown, _options: unknown) => undefined),
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

describe('useResearchSession', () => {
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
    runs.history.mockReset().mockResolvedValue([])
    runs.active.mockReset().mockResolvedValue(null)
    runs.stop.mockClear()
    runs.subscribe.mockReset().mockResolvedValue(0)
    window.localStorage.clear()
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

  it('保留研究摘要与工具活动的到达顺序，工具完成不改变位置', async () => {
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
        result_urls: ['https://a.example/x', 'https://b.example/y']
      })
      sdk.options?.onCustomEvent?.({
        type: 'progress', kind: 'activity', id: 'tool:b', stage: 'tool_agent',
        message: '已完成联网检索：搜索资金', status: 'completed',
        activity_type: 'web_search', group_id: 'web_search:1', task_id: 'b',
        query: '搜索资金', result_count: 2,
        result_urls: ['https://b.example/y', 'https://c.example/z']
      })
    })

    expect(result.current.messages.at(-1)?.progress).toEqual([
      expect.objectContaining({
        id: 'web_search:1',
        activityType: 'web_search',
        queryCount: 2,
        resultCount: 3,
        status: 'completed'
      })
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
