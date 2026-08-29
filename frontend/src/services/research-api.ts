import { parseSseEvent, type AgentProgress } from '../agent-stream'
import type { PersistedConversationMessage } from '../conversation-session'

export type RequestIdentity = {
  tenantId: string
  userId: string
  accessToken?: string
}

export type AgentResponse = {
  content: string
  trace_id: string
  cited_evidence_ids: string[]
  approval_status: string
}

export type StreamHandlers = {
  onThinking: (content: string) => void
  onProgress: (progress: AgentProgress) => void
  onAnswer: (content: string) => void
}

const apiBaseUrl = normalizeBaseUrl(import.meta.env.VITE_AGENT_API_BASE_URL)

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

export async function streamResearchMessage(
  sessionId: string,
  message: string,
  attachmentIds: string[],
  identity: RequestIdentity,
  handlers: StreamHandlers,
  signal?: AbortSignal
): Promise<AgentResponse> {
  const response = await fetch(
    `${apiBaseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages/stream`,
    {
      method: 'POST',
      signal,
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...authHeaders(identity)
      },
      body: JSON.stringify({ message, attachments: attachmentIds })
    }
  )
  if (!response.ok) return parseJsonResponse<AgentResponse>(response)
  if (!response.body) throw new Error('当前浏览器不支持流式回答。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResponse: AgentResponse | null = null

  const consume = (raw: string) => {
    const event = parseSseEvent(raw)
    if (!event) return
    if (event.event === 'thinking_delta') {
      const payload = JSON.parse(event.data) as { content?: string }
      if (payload.content) handlers.onThinking(payload.content)
    }
    if (event.event === 'progress') handlers.onProgress(JSON.parse(event.data) as AgentProgress)
    if (event.event === 'answer_delta') {
      const payload = JSON.parse(event.data) as { content?: string }
      if (payload.content) handlers.onAnswer(payload.content)
    }
    if (event.event === 'final') finalResponse = JSON.parse(event.data) as AgentResponse
    if (event.event === 'error') {
      const payload = JSON.parse(event.data) as { message?: string }
      throw new Error(payload.message ?? 'AI 研究任务执行失败。')
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() ?? ''
    events.forEach(consume)
  }
  buffer += decoder.decode()
  if (buffer.trim()) consume(buffer)
  if (!finalResponse) throw new Error('回答流已结束，但未收到最终结果。')
  return finalResponse
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
