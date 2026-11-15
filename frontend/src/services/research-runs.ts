import { agentRequestHeaders, type RequestIdentity } from './research-api'
import { isStreamAccessCode, RunSubscriptionAuthorizationError } from './run-subscription-error'

export type RunStatus = 'running' | 'stop_requested' | 'cancelled' | 'completed' | 'failed' | 'rejected' | 'interrupted'
export type ResearchRun = {
  run_id: string
  request_id?: string
  status: RunStatus
  message_id: string
  content: string
  last_event_sequence: number
  replay_after?: number
}
export type RunEvent = { sequence: number; event: string; data: unknown; transient?: boolean }

const baseUrl = typeof import.meta.env.VITE_AGENT_API_BASE_URL === 'string'
  ? import.meta.env.VITE_AGENT_API_BASE_URL.trim().replace(/\/$/, '') : ''

function runBase(sessionId: string): string {
  return `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/runs`
}

async function readResponse<T>(response: Response): Promise<T> {
  const body = await response.json()
  if (!response.ok) {
    const detail = body?.detail
    const code = typeof detail?.code === 'string' ? detail.code : `HTTP_${response.status}`
    const message = typeof detail === 'string' ? detail : detail?.message || detail?.code || `请求失败 · HTTP ${response.status}`
    if ([401,403,404].includes(response.status) || isStreamAccessCode(code) || code === 'permission_directory_unavailable') {
      throw new RunSubscriptionAuthorizationError(message, code)
    }
    throw new Error(message)
  }
  return body as T
}

export async function loadActiveRun(sessionId: string, identity: RequestIdentity, signal?: AbortSignal): Promise<ResearchRun | null> {
  const response = await fetch(`${runBase(sessionId)}/active`, { headers: agentRequestHeaders(identity), signal })
  if (response.status === 404) return null
  return (await readResponse<{ run: ResearchRun | null }>(response)).run
}

export async function stopResearchRun(sessionId: string, runId: string, identity: RequestIdentity): Promise<{ run_id: string; status: RunStatus }> {
  return readResponse(await fetch(`${runBase(sessionId)}/${encodeURIComponent(runId)}/stop`, {
    method: 'POST', headers: agentRequestHeaders(identity)
  }))
}

/** 订阅持久化事件。调用方保留游标；连接关闭本身不表示任务成功。 */
export async function subscribeRunEvents(
  sessionId: string, runId: string, identity: RequestIdentity, after: number,
  signal: AbortSignal, onEvent: (event: RunEvent) => void
): Promise<number> {
  const response = await fetch(`${runBase(sessionId)}/${encodeURIComponent(runId)}/events?after=${after}`, {
    headers: { ...agentRequestHeaders(identity), Accept: 'text/event-stream' }, signal
  })
  if (!response.ok) { await readResponse(response); return after }
  if (!response.body) throw new Error('服务未返回输出流')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let cursor = after
  const consume = (frame: string) => {
    let event = 'message'
    let sequence: number | undefined
    const data: string[] = []
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      if (line.startsWith('id:')) sequence = Number(line.slice(3).trim())
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (!data.length) return
    if (sequence !== undefined && (!Number.isSafeInteger(sequence) || sequence <= cursor)) return
    const payload: unknown = JSON.parse(data.join('\n'))
    if (event === 'error' && payload && typeof payload === 'object' && 'code' in payload && isStreamAccessCode(payload.code)) {
      const message = 'message' in payload && typeof payload.message === 'string' ? payload.message : '当前订阅已停止，请确认权限后重连。'
      throw new RunSubscriptionAuthorizationError(message, payload.code)
    }
    if (sequence === undefined) {
      // 检查前的进度预览不推进恢复游标，正文和终态只能来自正式有序事件。
      const type = payload && typeof payload === 'object' && 'type' in payload ? payload.type : undefined
      if (event === 'custom' && (type === 'progress' || type === 'answer_sources')) {
        onEvent({ sequence: cursor, event, data: payload, transient: true })
      }
      return
    }
    onEvent({ sequence, event, data: payload })
    cursor = sequence
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      // CRLF 可能恰好在网络分块之间，保留末尾的 CR 等待下一块。
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        consume(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
      }
      if (done) break
    }
    return cursor
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
