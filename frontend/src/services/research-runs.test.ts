import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadActiveRun, stopResearchRun, subscribeRunEvents } from './research-runs'

afterEach(() => vi.unstubAllGlobals())

describe('持久化运行接口', () => {
  const identity = { tenantId: 't1', userId: 'u1' }
  it('分块解析 UTF-8 SSE，忽略心跳及重复序号', async () => {
    const bytes = new TextEncoder().encode(': ping\r\n\r\nid: 1\r\nevent: messages\r\ndata: [{"content":"中文"},{}]\r\n\r\nid: 1\nevent: messages\ndata: [{"content":"重复"},{}]\n\nid: 2\nevent: custom\ndata: {"type":"run_status","status":"completed"}\n\n')
    const body = new ReadableStream({ start(controller) {
      for (let i = 0; i < bytes.length; i += 2) controller.enqueue(bytes.slice(i, i + 2))
      controller.close()
    } })
    const fetchMock = vi.fn(async (_url: string, _options: RequestInit) => new Response(body))
    vi.stubGlobal('fetch', fetchMock)
    const events: unknown[] = []
    const cursor = await subscribeRunEvents('s1', 'r1', identity, 0, new AbortController().signal, (event) => events.push(event))
    expect(cursor).toBe(2)
    expect(events).toHaveLength(2)
    expect(events[0]).toEqual({ sequence: 1, event: 'messages', data: [{ content: '中文' }, {}] })
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toMatchObject({ 'X-Tenant-ID': 't1', 'X-User-ID': 'u1' })
  })

  it('查询与停止使用同一会话身份，停止不只关闭本地连接', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ run: { run_id: 'r1', status: 'running' } })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ run_id: 'r1', status: 'stop_requested' })))
    vi.stubGlobal('fetch', fetchMock)
    expect((await loadActiveRun('s1', identity))?.run_id).toBe('r1')
    expect((await stopResearchRun('s1', 'r1', identity)).status).toBe('stop_requested')
    expect(fetchMock.mock.calls[1][1].method).toBe('POST')
  })

  it('接收无序号进度预览但不推进正文恢复游标，忽略无序号正文和终态', async () => {
    const body = new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode(
        'id: 2\nevent: custom\ndata: {"type":"run_started"}\n\n' +
        'event: custom\ndata: {"type":"progress","message":"检查中"}\n\n' +
        'event: custom\ndata: {"type":"answer_sources","sources":[]}\n\n' +
        'event: messages\ndata: [{"content":"不可提前展示"},{}]\n\n' +
        'event: custom\ndata: {"type":"run_status","status":"completed"}\n\n' +
        'event: custom\ndata: {"type":"final","response":{"content":"不可提前展示"}}\n\n'
      ))
      controller.close()
    } })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
    const events: unknown[] = []
    const cursor = await subscribeRunEvents('s1', 'r1', identity, 1, new AbortController().signal, event => events.push(event))
    expect(cursor).toBe(2)
    expect(events).toEqual([
      { sequence:2, event:'custom', data:{ type:'run_started' } },
      { sequence:2, event:'custom', transient:true, data:{ type:'progress', message:'检查中' } },
      { sequence:2, event:'custom', transient:true, data:{ type:'answer_sources', sources:[] } }
    ])
  })

  it('无权读取或停止时传播服务器错误', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: { code: 'run_not_found' } }), { status: 404 })))
    await expect(stopResearchRun('s1', 'r1', identity)).rejects.toThrow('run_not_found')
  })

  it.each(['stream_access_denied', 'stream_access_expired', 'stream_access_unavailable'])(
    '固定授权终止错误 %s 不推进游标或发布后续正文', async (code) => {
      const body = new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode(
          'id: 1\nevent: custom\ndata: {"type":"run_started"}\n\n' +
          `event: error\ndata: {"code":"${code}","message":"请重新连接","retryable":false}\n\n` +
          'id: 2\nevent: messages\ndata: [{"content":"禁止继续显示"},{}]\n\n'
        ))
        controller.close()
      } })
      vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
      const events: unknown[] = []
      await expect(subscribeRunEvents('s1', 'r1', identity, 0, new AbortController().signal, event => events.push(event)))
        .rejects.toMatchObject({ name:'RunSubscriptionAuthorizationError', code })
      expect(events).toHaveLength(1)
      expect(events[0]).toMatchObject({ sequence:1, event:'custom' })
    }
  )

  it.each([401,403,404])('HTTP %s 是授权/资源终止，不当作临时断线', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail:{ code:'run_not_found' } }), { status })))
    await expect(subscribeRunEvents('s1','r1',identity,0,new AbortController().signal,()=>{}))
      .rejects.toMatchObject({ name:'RunSubscriptionAuthorizationError' })
  })
})
