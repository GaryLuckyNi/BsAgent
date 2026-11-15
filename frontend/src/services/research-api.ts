import type { PersistedConversationMessage } from '../conversation-session'

export type RequestIdentity = {
  tenantId: string
  userId: string
  accessToken?: string
}

export type ResearchSessionSummary = {
  session_id: string
  title: string
  preview: string
  updated_at: string
}

export async function loadResearchSessions(identity: RequestIdentity, query = ''): Promise<ResearchSessionSummary[]> {
  const params = new URLSearchParams({ limit: '100' })
  if (query.trim()) params.set('q', query.trim())
  const response = await fetch(`${apiBaseUrl}/api/sessions?${params}`, { headers: authHeaders(identity) })
  const body = await parseJsonResponse<{ items: ResearchSessionSummary[] }>(response)
  return body.items
}

export async function deleteResearchSession(sessionId: string, identity: RequestIdentity): Promise<void> {
  const response = await fetch(`${apiBaseUrl}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE', headers: authHeaders(identity)
  })
  if (response.ok) return
  const body = await response.json().catch(() => null)
  const code = body?.detail?.code
  if (code === 'run_stop_not_supported') throw new Error('当前任务暂不支持停止，请等待任务结束后重试删除。')
  if (code === 'session_changed_retry') throw new Error('会话状态发生变化，请重试删除。')
  throw new Error(`删除研究记录失败 · HTTP ${response.status}`)
}

const apiBaseUrl = normalizeBaseUrl(import.meta.env.VITE_AGENT_API_BASE_URL)

export function agentProtocolStreamUrl(sessionId: string): string {
  return `${apiBaseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages/protocol-stream`
}

export function agentRequestHeaders(identity: RequestIdentity): Record<string, string> {
  return authHeaders(identity)
}

export async function loadSessionMessages(
  sessionId: string,
  identity: RequestIdentity
): Promise<PersistedConversationMessage[]> {
  const response = await fetch(
    `${apiBaseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=200`,
    { headers: authHeaders(identity) }
  )
  if (response.status === 404) return []
  const body = await parseJsonResponse<{ items: PersistedConversationMessage[] }>(response)
  return body.items
}

export async function checkAgentHealth(): Promise<boolean> {
  try {
    return (await fetch(`${apiBaseUrl}/health/ready`)).ok
  } catch {
    return false
  }
}

export async function uploadImage(
  file: File,
  sessionId: string,
  identity: RequestIdentity
): Promise<{ asset_id: string; status: string }> {
  const form = new FormData()
  form.append('file', file)
  form.append('session_id', sessionId)
  return parseJsonResponse(
    await fetch(`${apiBaseUrl}/api/assets/images`, {
      method: 'POST',
      headers: authHeaders(identity),
      body: form
    })
  )
}

function authHeaders(identity: RequestIdentity): Record<string, string> {
  return identity.accessToken
    ? { Authorization: `Bearer ${identity.accessToken}` }
    : { 'X-Tenant-ID': identity.tenantId, 'X-User-ID': identity.userId }
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const text = await response.text()
  const body = text ? (JSON.parse(text) as unknown) : null
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null
    throw new Error(typeof detail === 'string' ? detail : `请求失败 · HTTP ${response.status}`)
  }
  return body as T
}

function normalizeBaseUrl(value: unknown): string {
  return typeof value === 'string' ? value.trim().replace(/\/$/, '') : ''
}
