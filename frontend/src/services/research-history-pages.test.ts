import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { loadResearchHistoryPage } from './research-history-pages'

const request = vi.fn()
beforeEach(() => { request.mockReset(); vi.stubGlobal('fetch', request) })
afterEach(() => vi.unstubAllGlobals())

describe('历史消息分页请求', () => {
  it('使用身份和取消信号请求200条更早消息', async () => {
    const items = [{ message_id: 'old', sequence_no: 3 }]
    request.mockResolvedValue(new Response(JSON.stringify({ items }), { status: 200 }))
    const controller = new AbortController()
    expect(await loadResearchHistoryPage('session/old', { tenantId: 't', userId: 'u' }, 401, controller.signal)).toEqual(items)
    expect(request).toHaveBeenCalledWith('/api/sessions/session%2Fold/messages?limit=200&before_sequence=401', {
      headers: { 'X-Tenant-ID': 't', 'X-User-ID': 'u' }, signal: controller.signal
    })
  })

  it('首次分页使用token身份，未提供游标时读取最近页', async () => {
    request.mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 }))
    await loadResearchHistoryPage('s', { tenantId: 't', userId: 'u', accessToken: 'token' })
    expect(request).toHaveBeenCalledWith('/api/sessions/s/messages?limit=200', {
      headers: { Authorization: 'Bearer token' }, signal: undefined
    })
  })

  it('会话404抛出中文错误，不能当空页成功', async () => {
    request.mockResolvedValue(new Response('', { status: 404 }))
    await expect(loadResearchHistoryPage('missing', { tenantId: 't', userId: 'u' })).rejects.toThrow('未找到目标历史会话')
  })

  it('分页失败显示中文请求错误', async () => {
    request.mockResolvedValue(new Response('', { status: 500 }))
    await expect(loadResearchHistoryPage('s', { tenantId: 't', userId: 'u' })).rejects.toThrow('加载历史消息失败 · HTTP 500')
  })
})
