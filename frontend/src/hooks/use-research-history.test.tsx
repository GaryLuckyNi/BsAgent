// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resolveConversationSession } from '../conversation-session'
import { useResearchHistory } from './use-research-history'

const api = vi.hoisted(() => ({ list: vi.fn(), delete: vi.fn() }))
vi.mock('../services/research-api', () => ({ loadResearchSessions: api.list, deleteResearchSession: api.delete }))
const identity = { tenantId: 't1', userId: 'u1' }
const records = [
  { session_id: 'real-1', title: '首条实际问题', preview: '真实回答摘要', updated_at: '2026-10-28T01:00:00Z' },
  { session_id: 'real-2', title: '另一条问题', preview: '另一个回答', updated_at: '2026-10-27T01:00:00Z' }
]

beforeEach(() => {
  const values = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', { configurable: true, value: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value)
  } })
  api.list.mockReset().mockResolvedValue(records)
  api.delete.mockReset().mockResolvedValue(undefined)
})
afterEach(cleanup)

describe('真实研究历史', () => {
  it('结果选择具体会话与消息，完整最近列表不变', async () => {
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    const match = { session_id: 'real-2', message_id: 'm2', sequence_no: 2, title: '另一条问题', snippet: '匹配消息', created_at: '2026-10-27T01:00:00Z' }
    act(() => result.current.navigateToMessage(match))
    expect(result.current.navigationTarget).toMatchObject({ sessionId: 'real-2', messageId: 'm2', sequenceNo: 2 })
    expect(result.current.activeThreadId).toBe('real-2')
    expect(resolveConversationSession(window.localStorage, identity, () => 'wrong')).toBe('real-2')
    expect(result.current.threads).toHaveLength(2)
    const requestId = result.current.navigationTarget?.requestId
    act(() => result.current.navigateToMessage(match))
    expect(result.current.navigationTarget?.requestId).not.toBe(requestId)
    act(() => result.current.activateThread('real-1'))
    expect(result.current.navigationTarget).toBeUndefined()
    await act(async () => { await result.current.deleteThread('real-1') })
    expect(result.current.activeThreadId).toBe('real-2')
  })

  it('较早会话仅缓存标题，不伪造最近列表，新建清除导航目标', async () => {
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    act(() => result.current.navigateToMessage({ session_id: 'older', message_id: 'old', sequence_no: 1, title: '更早研究', snippet: '旧内容', created_at: '2025-01-01T00:00:00Z' }))
    expect(result.current.activeThread?.title).toBe('更早研究')
    expect(result.current.threads.map((item) => item.id)).toEqual(['real-1', 'real-2'])
    await act(async () => { await result.current.refresh() })
    expect(result.current.activeThread?.title).toBe('更早研究')
    act(() => { result.current.createThread() })
    expect(result.current.navigationTarget).toBeUndefined()
  })

  it('旧会话实际提交后进入最近列表，并保留搜索返回的首问标题', async () => {
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    act(() => result.current.navigateToMessage({ session_id: 'older', message_id: 'old', sequence_no: 1, title: '旧会话首问', snippet: '旧内容', created_at: '2025-01-01T00:00:00Z' }))
    act(() => result.current.updateConversation('older', [{ id: 'new', role: 'user', content: '最新追问', createdAt: '2026-10-28T06:00:00Z', status: 'complete' }]))
    expect(result.current.threads[0]).toMatchObject({ id: 'older', title: '旧会话首问', preview: '最新追问' })
  })

  it('删除失败保留记录，删除最后一条后进入空白研究', async () => {
    api.list.mockResolvedValue([records[0]])
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(1))
    act(() => result.current.activateThread('real-1'))
    api.delete.mockRejectedValueOnce(new Error('删除失败'))
    await act(async () => { await expect(result.current.deleteThread('real-1')).rejects.toThrow('删除失败') })
    expect(result.current.threads).toHaveLength(1)
    await act(async () => { await result.current.deleteThread('real-1') })
    expect(result.current.threads).toHaveLength(0)
    expect(result.current.activeThreadId).not.toBe('real-1')
    expect(result.current.activeThread).toBeUndefined()
  })

  it('删除期间切换会话不会抢回选择，旧刷新结果与旧消息不能复活记录', async () => {
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    act(() => result.current.activateThread('real-1'))
    let finishDelete!: () => void
    api.delete.mockImplementation(() => new Promise<void>((resolve) => { finishDelete = resolve }))
    let pendingDelete!: Promise<void>
    act(() => { pendingDelete = result.current.deleteThread('real-1') })
    act(() => result.current.activateThread('real-2'))
    await act(async () => { finishDelete(); await pendingDelete })
    await act(async () => { await result.current.refresh() })
    act(() => result.current.updateConversation('real-1', [{ id: 'late', role: 'user', content: '迟到消息', createdAt: '2026-10-28T05:00:00Z', status: 'complete' }]))
    expect(result.current.activeThreadId).toBe('real-2')
    expect(result.current.threads.some((item) => item.id === 'real-1')).toBe(false)
  })

  it('删除期间的刷新保留完整列表且不会复活已删除会话', async () => {
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    let finishRefresh!: (items: typeof records) => void
    api.list.mockImplementation(() => new Promise<typeof records>((resolve) => { finishRefresh = resolve }))
    let refreshing!: Promise<void>
    act(() => { refreshing = result.current.refresh() })
    await act(async () => { await result.current.deleteThread('real-1') })
    await act(async () => { finishRefresh(records); await refreshing })
    expect(result.current.threads.map((item) => item.id)).toEqual(['real-2'])
    expect(api.list).toHaveBeenLastCalledWith(identity)
  })
  it('读取真实记录，选择会话后持久化并让标题与会话一致', async () => {
    const { result, unmount } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    expect(result.current.threads[0]).toMatchObject({ id: 'real-1', title: '首条实际问题', preview: '真实回答摘要' })
    act(() => result.current.activateThread('real-2'))
    expect(result.current.activeThread?.title).toBe('另一条问题')
    expect(resolveConversationSession(window.localStorage, identity, () => 'wrong')).toBe('real-2')
    unmount()
    const restored = renderHook(() => useResearchHistory(identity))
    expect(restored.result.current.activeThreadId).toBe('real-2')
  })

  it('新建为空白真实会话，首问加入列表且不混入演示数据', async () => {
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    act(() => { result.current.createThread() })
    expect(result.current.activeThread).toBeUndefined()
    const id = result.current.activeThreadId
    act(() => result.current.updateConversation(id, [{ id: 'msg1', role: 'user', content: '当前真实提问', createdAt: '2026-10-28T03:00:00Z', status: 'complete' }]))
    expect(result.current.activeThread).toMatchObject({ id, title: '当前真实提问', preview: '当前真实提问' })
    expect(result.current.threads[0].id).toBe(id)
    expect(result.current.threads.some((thread) => thread.id === 'market-outlook')).toBe(false)
  })

  it('长会话提交后保留服务端首问，完成刷新后使用真实更新时间与回答摘要', async () => {
    const { result } = renderHook(() => useResearchHistory(identity))
    await waitFor(() => expect(result.current.threads).toHaveLength(2))
    act(() => result.current.activateThread('real-1'))
    act(() => result.current.updateConversation('real-1', [
      { id: 'middle', role: 'user', content: '尾部分页中的中途问题', createdAt: '2026-10-27T01:00:00Z', status: 'complete' },
      { id: 'new', role: 'user', content: '当前追问', createdAt: '2026-10-28T04:00:00Z', status: 'complete' }
    ]))
    expect(result.current.activeThread?.title).toBe('首条实际问题')
    api.list.mockResolvedValue([{ ...records[0], preview: '刚完成的真实回答', updated_at: '2026-10-28T04:05:00Z' }, records[1]])
    await act(async () => { await result.current.refresh() })
    expect(result.current.activeThread).toMatchObject({ title: '首条实际问题', preview: '刚完成的真实回答', updatedAt: '2026-10-28T04:05:00Z' })
  })
})
