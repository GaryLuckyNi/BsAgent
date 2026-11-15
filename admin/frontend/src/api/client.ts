import axios from 'axios'
import { authMode, authState, invalidateAuthentication } from '../auth'

export const http = axios.create({
  baseURL: import.meta.env.VITE_ADMIN_API_BASE_URL || '',
  timeout: 15_000
})

http.interceptors.request.use((config) => {
  if (authMode === 'mock') return config
  const token = authState.value.accessToken
  if (authState.value.status !== 'authenticated' || !token) throw new Error('请先登录。')
  const expected = new URL(http.defaults.baseURL || '/', window.location.origin)
  const target = new URL(config.url || '', new URL(config.baseURL || '/', window.location.origin))
  if (target.origin !== expected.origin) throw new Error('配置请求的目标地址无效。')
  config.headers.set('Authorization', `Bearer ${token}`)
  return config
})
http.interceptors.response.use((response) => response, (error) => {
  if (error.response?.status === 401 && authMode !== 'mock') invalidateAuthentication()
  return Promise.reject(error)
})

export function apiErrorMessage(error: unknown): string {
  if (!axios.isAxiosError(error)) {
    return error instanceof Error ? error.message : '请求失败'
  }

  if (!error.response) {
    return '无法连接 Admin 后端，请确认服务已启动'
  }

  const detail = error.response.data?.detail
  if (typeof detail === 'string') {
    return detail
  }
  if (typeof detail?.code === 'string') {
    if (error.response.status === 401) return '登录已失效，请重新登录。'
    if (error.response.status === 403) return '当前账号没有执行此配置操作的权限。'
    return '配置服务暂不可用，请稍后重试。'
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const path = Array.isArray(item.loc) ? item.loc.join('.') : ''
        return path ? `${path}: ${item.msg}` : item.msg
      })
      .join('；')
  }
  return `请求失败：HTTP ${error.response.status}`
}
