// @vitest-environment jsdom

import type { Message } from '@langchain/langgraph-sdk'
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useResearchSession } from './use-research-session'

const sdk = vi.hoisted(() => ({
  messages: [] as Message[],
  cloneMessagesOnRead: false,
  options: undefined as {
    throttle?: boolean | number
    onCustomEvent?: (event: Record<string, unknown>) => void
  } | undefined,
  submit: vi.fn(async (_input: unknown, _options: unknown) => undefined),
  stop: vi.fn(async () => undefined)
}))

vi.mock('@langchain/langgraph-sdk/react', () => ({
  FetchStreamTransport: class FetchStreamTransport {},
  useStream: (options: {
    throttle?: boolean | number
    onCustomEvent?: (event: Record<string, unknown>) => void
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
  loadSessionMessages: async () => []
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
    window.localStorage.clear()
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

  it('关闭 SDK 批处理以立即交付首个 token', () => {
    renderHook(() => useResearchSession({
      tenantId: 'tenant-1',
      userId: 'user-1'
    }))

    expect(sdk.options?.throttle).toBe(false)
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

  it('重复 token 和空白均原样累加，不按文本内容去重', async () => {
    const { result } = renderHook(() => useResearchSession({ tenantId: 'tenant-1', userId: 'user-1' }))
    await act(async () => { await result.current.sendMessage('测试推理流。') })
    act(() => {
      for (const content of ['哈', '哈', '\n', '\n', '结束']) {
        sdk.options?.onCustomEvent?.({ type: 'thinking_delta', id: 'analysis:1', content })
      }
      sdk.options?.onCustomEvent?.({ type: 'thinking_delta', id: 'analysis:2', content: '哈哈\n\n结束' })
    })
    expect(result.current.messages.at(-1)?.reasoning?.map((item) => item.content))
      .toEqual(['哈哈\n\n结束', '哈哈\n\n结束'])
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

  it('保留原始思考与工具活动的交错到达顺序', async () => {
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
    expect(message.reasoning?.map((item) => item.sequence)).toEqual([0, 2])
    expect(message.progress?.map((item) => item.sequence)).toEqual([1])
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
})
