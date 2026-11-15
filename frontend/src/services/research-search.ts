import { agentRequestHeaders, type RequestIdentity } from './research-api'

export type ResearchSearchResult = {
  session_id: string
  message_id: string
  title: string
  snippet: string
  role: 'user' | 'assistant'
  sequence_no: number
  created_at: string
}

export type ResearchSearchPage = { items: ResearchSearchResult[]; next_offset: number | null }

export async function searchResearchMessages(identity: RequestIdentity, query: string, offset = 0, signal?: AbortSignal): Promise<ResearchSearchPage> {
  const baseUrl = (import.meta.env.VITE_AGENT_API_BASE_URL || '').trim().replace(/\/$/, '')
  const params = new URLSearchParams({ q: query.trim(), limit: '30', offset: String(offset) })
  const response = await fetch(`${baseUrl}/api/sessions/search?${params}`, { headers: agentRequestHeaders(identity), signal })
  if (!response.ok) throw new Error('搜索暂时不可用，请重试。')
  return response.json()
}
