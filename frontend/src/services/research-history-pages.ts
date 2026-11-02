import type { PersistedConversationMessage } from '../conversation-session'
import { agentRequestHeaders, type RequestIdentity } from './research-api'

const apiBaseUrl = String(import.meta.env.VITE_AGENT_API_BASE_URL || '').trim().replace(/\/+$/, '')

export async function loadResearchHistoryPage(
  sessionId: string, identity: RequestIdentity, beforeSequence?: number, signal?: AbortSignal
): Promise<PersistedConversationMessage[]> {
  const params = new URLSearchParams({ limit: '200' })
  if (beforeSequence !== undefined) params.set('before_sequence', String(beforeSequence))
  const response = await fetch(`${apiBaseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages?${params}`, {
    headers: agentRequestHeaders(identity), signal
  })
  if (response.status === 404) throw new Error('未找到目标历史会话，会话可能已被删除。')
  if (!response.ok) throw new Error(`加载历史消息失败 · HTTP ${response.status}`)
  const body = await response.json() as { items: PersistedConversationMessage[] }
  return body.items
}
