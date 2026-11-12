// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'

const oidc = vi.hoisted(() => ({ loaded: null, expired: null, user: null }))
vi.mock('oidc-client-ts', () => ({
  WebStorageStateStore: class {},
  UserManager: class {
    events = { addUserLoaded: (cb) => { oidc.loaded = cb }, addUserUnloaded() {},
      addAccessTokenExpired: (cb) => { oidc.expired = cb }, addSilentRenewError() {} }
    getUser = async () => oidc.user
    signinRedirect = vi.fn()
    signoutRedirect = vi.fn()
  }
}))

afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); oidc.user = null; delete window.__ADMIN_RUNTIME_CONFIG__ })

async function load() {
  vi.resetModules()
  vi.stubEnv('VITE_ADMIN_AUTH_MODE', 'jwt')
  window.__ADMIN_RUNTIME_CONFIG__ = { oidcAuthority: 'https://idp.test', oidcClientId: 'admin-browser' }
  return import('../src/auth')
}

it('匿名用户不能打开配置，也不发起配置请求', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch')
  const auth = await load()
  await auth.initializeAuthentication()
  expect(auth.canUseAdmin.value).toBe(false)
  expect(auth.authState.value.status).toBe('anonymous')
  expect(fetch).not.toHaveBeenCalled()
})

it('登录后由服务端确认配置权限，token 过期立即移除访问', async () => {
  oidc.user = { access_token: 'user-token', expired: false, profile: { sub: 'u', name: '管理员' } }
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ permissions: ['config:read'] }), { status: 200 }))
  const auth = await load()
  await auth.initializeAuthentication()
  await vi.waitFor(() => expect(auth.canUseAdmin.value).toBe(true))
  expect(fetch.mock.calls[0][1].headers.Authorization).toBe('Bearer user-token')
  oidc.expired()
  expect(auth.canUseAdmin.value).toBe(false)
  expect(auth.authState.value.accessToken).toBe('')
})

it('后端拒绝配置权限时不打开页面', async () => {
  oidc.user = { access_token: 'user-token', expired: false, profile: { sub: 'u' } }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 403 }))
  const auth = await load()
  await auth.initializeAuthentication()
  await vi.waitFor(() => expect(auth.authState.value.error).toContain('配置'))
  expect(auth.canUseAdmin.value).toBe(false)
})

it('API 客户端注入用户 token，并拒绝向其他站点发送', async () => {
  oidc.user = { access_token: 'user-token', expired: false, profile: { sub: 'u' } }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ permissions: ['config:read'] }), { status: 200 }))
  const auth = await load()
  await auth.initializeAuthentication()
  const { http } = await import('../src/api/client')
  const received = []
  http.defaults.adapter = async (config) => {
    received.push(config)
    return { status: 200, data: {}, statusText: 'OK', headers: {}, config }
  }
  await http.get('/api/skills')
  expect(received[0].headers.get('Authorization')).toBe('Bearer user-token')
  await expect(http.get('https://evil.test/api/skills')).rejects.toThrow('目标地址')
  expect(received).toHaveLength(1)
})

it('API 返回 401 后清除 token 和配置访问状态', async () => {
  oidc.user = { access_token: 'user-token', expired: false, profile: { sub: 'u' } }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ permissions: ['config:read'] }), { status: 200 }))
  const auth = await load()
  await auth.initializeAuthentication()
  const { http } = await import('../src/api/client')
  http.defaults.adapter = async () => { throw { response: { status: 401 } } }
  await expect(http.get('/api/skills')).rejects.toBeDefined()
  expect(auth.canUseAdmin.value).toBe(false)
  expect(auth.authState.value.accessToken).toBe('')
})
