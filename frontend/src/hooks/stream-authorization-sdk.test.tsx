// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useResearchSession } from './use-research-session'

const runs = vi.hoisted(() => ({ active:vi.fn(), subscribe:vi.fn() }))
vi.mock('../services/research-runs', () => ({
  loadActiveRun:runs.active, subscribeRunEvents:runs.subscribe, stopResearchRun:vi.fn()
}))
vi.mock('../services/research-api', () => ({
  agentProtocolStreamUrl:()=> 'https://stream.example.test/initial', agentRequestHeaders:()=>({}),
  checkAgentHealth:async()=>true, loadSessionMessages:async()=>[]
}))
vi.mock('../services/research-history-pages', () => ({ loadResearchHistoryPage:vi.fn() }))

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
beforeEach(() => {
  runs.active.mockReset().mockResolvedValue(null)
  runs.subscribe.mockReset().mockResolvedValue(0)
  vi.spyOn(console, 'error').mockImplementation(()=>{})
})

describe('真实 FetchStreamTransport / SDK 的初次订阅授权拒绝', () => {
  it.each(['stream_access_denied','stream_access_expired','stream_access_unavailable'])(
    '%s 不触发 GET 恢复或伪造终态', async code => {
      const bytes = new TextEncoder().encode(
        'event: custom\ndata: {"type":"run_started","run_id":"r1","message_id":"a1"}\n\n' +
        'event: messages\ndata: [{"type":"ai","id":"a1","content":"已检查的安全正文"},{}]\n\n' +
        `event: error\ndata: {"name":"RunSubscriptionAuthorizationError","code":"${code}","message":"当前订阅已停止","retryable":false}\n\n`
      )
      const fetchMock = vi.fn(async()=>new Response(new ReadableStream({start(controller){
        controller.enqueue(bytes); controller.close()
      }})))
      vi.stubGlobal('fetch', fetchMock)
      const { result } = renderHook(()=>useResearchSession({tenantId:'t',userId:'u'}, 's'))
      await act(async()=>{ await Promise.resolve(); await Promise.resolve() })
      runs.active.mockClear()
      await act(async()=>{ await result.current.sendMessage('公开问题') })
      await waitFor(()=>expect(result.current.error).toContain('当前订阅已停止'))
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(runs.active).not.toHaveBeenCalled()
      expect(runs.subscribe).not.toHaveBeenCalled()
      expect(result.current.messages.at(-1)).toMatchObject({content:'已检查的安全正文',status:'streaming'})
    }
  )

  it.each([401,403,404,503])('初次 HTTP %s 不自动查询/订阅后台任务', async status => {
    const code = status === 503 ? 'permission_directory_unavailable' : 'permission_denied'
    const fetchMock=vi.fn(async()=>new Response(JSON.stringify({detail:{code}}),{status}))
    vi.stubGlobal('fetch',fetchMock)
    const { result }=renderHook(()=>useResearchSession({tenantId:'t',userId:'u'},'s'))
    await act(async()=>{await Promise.resolve();await Promise.resolve()})
    runs.active.mockClear()
    await act(async()=>{await result.current.sendMessage('公开问题')})
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(runs.active).not.toHaveBeenCalled()
    expect(runs.subscribe).not.toHaveBeenCalled()
    expect(result.current.messages.at(-1)?.status).not.toBe('complete')
  })

  it('换用新凭证后允许正常的新订阅，不把拒绝永久带到下一次请求', async () => {
    const denied='event: custom\ndata: {"type":"run_started","run_id":"r1","message_id":"a1"}\n\n'+
      'event: error\ndata: {"name":"RunSubscriptionAuthorizationError","code":"stream_access_expired","message":"令牌已过期"}\n\n'
    const completed='event: custom\ndata: {"type":"run_started","run_id":"r2","message_id":"a2"}\n\n'+
      'event: custom\ndata: {"type":"final","response":{"content":"新订阅安全回答"}}\n\n'+
      'event: custom\ndata: {"type":"run_status","run_id":"r2","status":"completed"}\n\n'
    const response=(text:string)=>new Response(new ReadableStream({start(c){c.enqueue(new TextEncoder().encode(text));c.close()}}))
    const fetchMock=vi.fn().mockResolvedValueOnce(response(denied)).mockResolvedValueOnce(response(completed))
    vi.stubGlobal('fetch',fetchMock)
    const {result,rerender}=renderHook(({token})=>useResearchSession({tenantId:'t',userId:'u',accessToken:token},'s'),{initialProps:{token:'old-token'}})
    await act(async()=>{await Promise.resolve();await Promise.resolve()})
    await act(async()=>{await result.current.sendMessage('旧请求')})
    expect(result.current.error).toContain('令牌已过期')
    rerender({token:'new-token'})
    await act(async()=>{await Promise.resolve();await Promise.resolve()})
    await act(async()=>{await result.current.sendMessage('新请求')})
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(result.current.messages.at(-1)).toMatchObject({content:'新订阅安全回答',status:'complete'})
    expect(result.current.error).toBe('')
  })

  it('普通传输错误仍可查询持久化结果并恢复真实终态', async () => {
    const bytes=new TextEncoder().encode('event: custom\ndata: {"type":"run_started","run_id":"r1","message_id":"a1"}\n\n'+
      'event: error\ndata: {"name":"StreamError","code":"ordinary_network_failure","message":"普通断线"}\n\n')
    vi.stubGlobal('fetch',vi.fn(async()=>new Response(new ReadableStream({start(c){c.enqueue(bytes);c.close()}}))))
    runs.subscribe.mockImplementation(async (_s,_r,_i,_a,_sig,onEvent)=>{
      onEvent({sequence:2,event:'messages',data:[{id:'a1',type:'ai',content:'后端确认回答'},{}]})
      onEvent({sequence:3,event:'custom',data:{type:'run_status',run_id:'r1',status:'completed'}})
      return 3
    })
    const {result}=renderHook(()=>useResearchSession({tenantId:'t',userId:'u'},'s'))
    await act(async()=>{await Promise.resolve();await Promise.resolve()})
    await act(async()=>{await result.current.sendMessage('公开问题')})
    await waitFor(()=>expect(runs.subscribe).toHaveBeenCalled())
    expect(result.current.messages.at(-1)).toMatchObject({content:'后端确认回答',status:'complete'})
  })
})
