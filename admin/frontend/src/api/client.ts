import axios from 'axios'

export const http = axios.create({
  baseURL: import.meta.env.VITE_ADMIN_API_BASE_URL || '',
  timeout: 15_000
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
