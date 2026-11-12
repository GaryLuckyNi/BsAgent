import { computed, shallowRef } from 'vue'
import { OidcAuthController, type AuthSnapshot, type RuntimeConfig } from './oidc'

type AdminRuntime = RuntimeConfig & { authMode?: 'jwt' | 'mock' }
declare global { interface Window { __ADMIN_RUNTIME_CONFIG__?: AdminRuntime } }

const runtime = window.__ADMIN_RUNTIME_CONFIG__ || {}
export const authMode = runtime.authMode || import.meta.env.VITE_ADMIN_AUTH_MODE || 'jwt'
const empty = (status: AuthSnapshot['status']): AuthSnapshot => ({
  status, accessToken: '', tenantId: '', userId: '', displayName: '', error: ''
})
export const authState = shallowRef<AuthSnapshot>(empty(authMode === 'mock' ? 'disabled' : 'loading'))
export const permissions = shallowRef<string[]>([])
export const canUseAdmin = computed(() => authMode === 'mock' || (
  authState.value.status === 'authenticated' && permissions.value.includes('config:read')
))
let controller: OidcAuthController | null = null
let generation = 0
let verifying: Promise<void> = Promise.resolve()

export function invalidateAuthentication(message = '登录已失效，请重新登录。') {
  generation++
  permissions.value = []
  authState.value = { ...empty('anonymous'), error: message }
}

async function verifyPermissions(snapshot: AuthSnapshot, revision: number) {
  try {
    const response = await fetch(`${import.meta.env.VITE_ADMIN_API_BASE_URL || ''}/api/auth/me`, {
      headers: { Authorization: `Bearer ${snapshot.accessToken}` },
      credentials: 'omit', redirect: 'error', cache: 'no-store'
    })
    if (!response.ok) throw new Error('当前账号没有配置访问权限，或登录已失效。')
    const payload = await response.json()
    if (!Array.isArray(payload.permissions) || !payload.permissions.includes('config:read')) {
      throw new Error('当前账号没有配置读取权限。')
    }
    if (revision === generation) permissions.value = payload.permissions
  } catch (error) {
    if (revision === generation) {
      permissions.value = []
      authState.value = { ...empty('error'), error: error instanceof Error ? error.message : '配置授权验证失败。' }
    }
  }
}

export async function initializeAuthentication() {
  if (authMode === 'mock') return
  try {
    controller = new OidcAuthController(runtime, (snapshot) => {
      const revision = ++generation
      permissions.value = []
      authState.value = snapshot
      if (snapshot.status === 'authenticated') verifying = verifyPermissions(snapshot, revision)
    })
    if (!controller.enabled) throw new Error('尚未配置 Admin 登录，请联系管理员。')
    await controller.initialize()
    await verifying
  } catch (error) {
    authState.value = { ...empty('error'), error: error instanceof Error ? error.message : '登录失败。' }
  }
}

export async function login() {
  try { await controller?.login() } catch { invalidateAuthentication('无法打开登录页面。') }
}
export async function logout() {
  invalidateAuthentication('')
  try { await controller?.logout() } catch { invalidateAuthentication('退出失败，请关闭当前页面并重试。') }
}
