import './styles.css'

type AgentResponse = {
  content: string
  trace_id: string
  cited_evidence_ids: string[]
  approval_status: string
}

type TraceEvent = {
  trace_id: string
  node_name: string
  status: string
  latency_ms: number
  input_ref: string | null
  output_ref: string | null
  error: unknown
  metadata: Record<string, unknown>
}

type TraceArtifact = {
  trace_id: string
  node_name: string
  artifact_type: string
  payload: unknown
}

type TraceResponse = {
  trace_id: string
  events: TraceEvent[]
  artifacts: Record<string, TraceArtifact>
}

type ChatMessage = {
  role: 'user' | 'agent'
  content: string
  traceId?: string
  citedEvidenceIds?: string[]
  approvalStatus?: string
  streaming?: boolean
}

type RuntimeState = {
  tenantId: string
  userId: string
  sessionId: string
  messages: ChatMessage[]
  trace: TraceResponse | null
  health: 'checking' | 'online' | 'offline'
  busy: boolean
  error: string
  lastLatencyMs: number | null
}

const apiBaseUrl = normalizeBaseUrl(import.meta.env.VITE_AGENT_API_BASE_URL)

const state: RuntimeState = {
  tenantId: 'tenant_1',
  userId: 'user_1',
  sessionId: `session_${crypto.randomUUID().slice(0, 8)}`,
  messages: [],
  trace: null,
  health: 'checking',
  busy: false,
  error: '',
  lastLatencyMs: null
}

const app = document.querySelector<HTMLDivElement>('#app')
if (!app) {
  throw new Error('App root not found.')
}

app.innerHTML = `
  <main class="shell">
    <aside class="rail">
      <div class="brand">
        <span class="brand__mark"></span>
        <div>
          <h1>BsAgent</h1>
          <p>Agent Runtime Console</p>
        </div>
      </div>

      <section class="control-panel">
        <label>
          Tenant
          <input id="tenantId" autocomplete="off" />
        </label>
        <label>
          User
          <input id="userId" autocomplete="off" />
        </label>
        <label>
          Session
          <input id="sessionId" autocomplete="off" />
        </label>
      </section>

      <section class="system-card">
        <div class="system-card__row">
          <span>Backend</span>
          <span id="healthBadge" class="status-pill">Checking</span>
        </div>
        <div class="system-card__row">
          <span>Last latency</span>
          <strong id="latencyValue">-</strong>
        </div>
        <div class="system-card__row">
          <span>Trace events</span>
          <strong id="traceCount">0</strong>
        </div>
      </section>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div>
          <span class="eyebrow">LIVE AGENT LINK</span>
          <h2>对话与执行观测</h2>
        </div>
        <button id="refreshHealth" class="ghost-button" type="button">刷新状态</button>
      </header>

      <div class="console-grid">
        <section class="chat-panel">
          <div id="messageList" class="message-list"></div>
          <form id="messageForm" class="composer">
            <textarea id="messageInput" rows="3" placeholder="输入要交给 Agent 处理的问题"></textarea>
            <button id="sendButton" type="submit">发送</button>
          </form>
        </section>

        <aside class="trace-panel">
          <div class="trace-panel__header">
            <div>
              <span class="eyebrow">TRACE</span>
              <h3 id="traceTitle">等待运行</h3>
            </div>
          </div>
          <div id="traceTimeline" class="trace-timeline"></div>
          <pre id="artifactPreview" class="artifact-preview"></pre>
        </aside>
      </div>
    </section>
  </main>
`

const tenantInput = getElement<HTMLInputElement>('tenantId')
const userInput = getElement<HTMLInputElement>('userId')
const sessionInput = getElement<HTMLInputElement>('sessionId')
const messageForm = getElement<HTMLFormElement>('messageForm')
const messageInput = getElement<HTMLTextAreaElement>('messageInput')
const sendButton = getElement<HTMLButtonElement>('sendButton')
const refreshHealthButton = getElement<HTMLButtonElement>('refreshHealth')

tenantInput.value = state.tenantId
userInput.value = state.userId
sessionInput.value = state.sessionId

tenantInput.addEventListener('input', () => {
  state.tenantId = tenantInput.value.trim()
})
userInput.addEventListener('input', () => {
  state.userId = userInput.value.trim()
})
sessionInput.addEventListener('input', () => {
  state.sessionId = sessionInput.value.trim()
})
refreshHealthButton.addEventListener('click', () => {
  void checkHealth()
})
messageForm.addEventListener('submit', (event) => {
  event.preventDefault()
  void submitMessage()
})

render()
void checkHealth()

async function submitMessage() {
  const message = messageInput.value.trim()
  state.error = ''
  if (!message) {
    state.error = '请输入消息。'
    render()
    return
  }
  if (!state.tenantId || !state.userId || !state.sessionId) {
    state.error = 'Tenant、User 和 Session 都不能为空。'
    render()
    return
  }

  state.busy = true
  state.lastLatencyMs = null
  state.messages.push({ role: 'user', content: message })
  const agentMessage: ChatMessage = { role: 'agent', content: '', streaming: true }
  state.messages.push(agentMessage)
  messageInput.value = ''
  render()

  const startedAt = performance.now()
  try {
    const response = await streamAgentMessage(message, {
      onDelta: (content) => {
        agentMessage.content += content
        render()
      },
      onTraceEvents: (trace) => {
        state.trace = trace
        render()
      }
    })
    state.lastLatencyMs = Math.round(performance.now() - startedAt)
    agentMessage.streaming = false
    agentMessage.content = agentMessage.content || response.content
    agentMessage.traceId = response.trace_id
    agentMessage.citedEvidenceIds = response.cited_evidence_ids
    agentMessage.approvalStatus = response.approval_status
    state.trace = await getTrace(response.trace_id)
  } catch (error) {
    agentMessage.streaming = false
    if (!agentMessage.content) {
      state.messages = state.messages.filter((item) => item !== agentMessage)
    }
    state.error = apiErrorMessage(error)
  } finally {
    state.busy = false
    render()
  }
}

async function streamAgentMessage(
  message: string,
  handlers: {
    onDelta: (content: string) => void
    onTraceEvents: (trace: TraceResponse) => void
  }
): Promise<AgentResponse> {
  const response = await fetch(
    `${apiBaseUrl}/api/sessions/${encodeURIComponent(state.sessionId)}/messages/stream`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream'
      },
      body: JSON.stringify({
        tenant_id: state.tenantId,
        user_id: state.userId,
        message
      })
    }
  )
  if (!response.ok) {
    return parseJsonResponse<AgentResponse>(response)
  }
  if (!response.body) {
    throw new Error('浏览器不支持读取流式响应。')
  }

  let finalResponse: AgentResponse | null = null
  const decoder = new TextDecoder()
  const reader = response.body.getReader()
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) {
      break
    }
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() ?? ''
    for (const rawEvent of events) {
      const parsed = parseSseEvent(rawEvent)
      if (!parsed) {
        continue
      }
      if (parsed.event === 'answer_delta') {
        const payload = JSON.parse(parsed.data) as { content?: string }
        if (payload.content) {
          handlers.onDelta(payload.content)
        }
      }
      if (parsed.event === 'trace_events') {
        const payload = JSON.parse(parsed.data) as {
          trace_id: string
          events: TraceEvent[]
        }
        handlers.onTraceEvents({
          trace_id: payload.trace_id,
          events: payload.events,
          artifacts: {}
        })
      }
      if (parsed.event === 'final') {
        finalResponse = JSON.parse(parsed.data) as AgentResponse
      }
      if (parsed.event === 'error') {
        const payload = JSON.parse(parsed.data) as {
          code?: string
          node?: string
          message?: string
        }
        throw new Error(
          [payload.code, payload.node, payload.message].filter(Boolean).join(' · ')
        )
      }
    }
  }

  buffer += decoder.decode()
  if (buffer.trim()) {
    const parsed = parseSseEvent(buffer)
    if (parsed?.event === 'final') {
      finalResponse = JSON.parse(parsed.data) as AgentResponse
    }
  }
  if (!finalResponse) {
    throw new Error('流式响应结束，但没有收到 final 事件。')
  }
  return finalResponse
}

async function getTrace(traceId: string): Promise<TraceResponse> {
  const response = await fetch(`${apiBaseUrl}/api/traces/${encodeURIComponent(traceId)}`)
  return parseJsonResponse<TraceResponse>(response)
}

async function checkHealth() {
  state.health = 'checking'
  render()
  try {
    const response = await fetch(`${apiBaseUrl}/health`)
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }
    state.health = 'online'
  } catch {
    state.health = 'offline'
  } finally {
    render()
  }
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const text = await response.text()
  const body = text ? JSON.parse(text) : null
  if (!response.ok) {
    throw new ApiError(response.status, body)
  }
  return body as T
}

function render() {
  tenantInput.value = state.tenantId
  userInput.value = state.userId
  sessionInput.value = state.sessionId
  sendButton.disabled = state.busy
  sendButton.textContent = state.busy ? '运行中' : '发送'

  renderHealth()
  renderMessages()
  renderTrace()
}

function renderHealth() {
  const healthBadge = getElement<HTMLSpanElement>('healthBadge')
  healthBadge.className = `status-pill status-pill--${state.health}`
  healthBadge.textContent =
    state.health === 'online' ? 'Online' : state.health === 'offline' ? 'Offline' : 'Checking'

  getElement<HTMLElement>('latencyValue').textContent =
    state.lastLatencyMs === null ? '-' : `${state.lastLatencyMs} ms`
  getElement<HTMLElement>('traceCount').textContent = String(state.trace?.events.length ?? 0)
}

function renderMessages() {
  const messageList = getElement<HTMLDivElement>('messageList')
  const rows = state.messages
    .map((message) => {
      const meta = message.traceId
        ? `<div class="message__meta">trace ${escapeHtml(message.traceId)} · approval ${
            escapeHtml(message.approvalStatus ?? '-')
          }</div>`
        : ''
      const evidence = message.citedEvidenceIds?.length
        ? `<div class="message__evidence">${message.citedEvidenceIds
            .map((id) => `<span>${escapeHtml(id)}</span>`)
            .join('')}</div>`
        : ''
      const stream = message.streaming ? '<span class="stream-cursor"></span>' : ''
      return `
        <article class="message message--${message.role}">
          <div class="message__role">${message.role === 'user' ? 'USER' : 'AGENT'}</div>
          <p>${escapeHtml(message.content)}${stream}</p>
          ${meta}
          ${evidence}
        </article>
      `
    })
    .join('')

  const error = state.error ? `<div class="error-banner">${escapeHtml(state.error)}</div>` : ''
  const empty = state.messages.length
    ? ''
    : '<div class="empty-state">等待第一条真实请求。</div>'
  messageList.innerHTML = `${empty}${rows}${error}`
  messageList.scrollTop = messageList.scrollHeight
}

function renderTrace() {
  const traceTitle = getElement<HTMLHeadingElement>('traceTitle')
  const timeline = getElement<HTMLDivElement>('traceTimeline')
  const artifactPreview = getElement<HTMLPreElement>('artifactPreview')

  if (!state.trace) {
    traceTitle.textContent = '等待运行'
    timeline.innerHTML = '<div class="empty-state">Trace 会在请求完成后显示。</div>'
    artifactPreview.textContent = ''
    return
  }

  traceTitle.textContent = state.trace.trace_id
  timeline.innerHTML = state.trace.events
    .map((event) => {
      const status = event.status === 'success' ? 'ok' : 'warn'
      const summary = typeof event.metadata?.summary === 'string' ? event.metadata.summary : ''
      return `
        <button class="trace-event trace-event--${status}" type="button" data-ref="${
          event.output_ref ?? ''
        }">
          <span>
            <b>${escapeHtml(event.node_name)}</b>
            ${summary ? `<small>${escapeHtml(summary)}</small>` : ''}
          </span>
          <strong>${escapeHtml(event.status)}</strong>
          <em>${event.latency_ms} ms</em>
        </button>
      `
    })
    .join('')

  timeline.querySelectorAll<HTMLButtonElement>('.trace-event').forEach((button) => {
    button.addEventListener('click', () => {
      const ref = button.dataset.ref ?? ''
      const artifact = state.trace?.artifacts[ref]
      artifactPreview.textContent = artifact
        ? JSON.stringify(artifact.payload, null, 2)
        : '该节点已执行。完整 artifact 会在最终 trace 加载完成后显示。'
    })
  })

  const firstArtifactRef = state.trace.events.find((event) => event.output_ref)?.output_ref
  const firstArtifact = firstArtifactRef ? state.trace.artifacts[firstArtifactRef] : null
  artifactPreview.textContent = firstArtifact
    ? JSON.stringify(firstArtifact.payload, null, 2)
    : '节点事件已返回，完整 artifact 会在最终 trace 加载完成后显示。'
}

function apiErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = error.body?.detail
    if (typeof detail === 'string') {
      return detail
    }
    if (detail && typeof detail === 'object' && 'message' in detail) {
      return String(detail.message)
    }
    if (detail && typeof detail === 'object' && 'code' in detail) {
      return `${String(detail.code)} · HTTP ${error.status}`
    }
    return `请求失败 · HTTP ${error.status}`
  }
  return error instanceof Error ? error.message : '请求失败'
}

function normalizeBaseUrl(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) {
    return ''
  }
  return value.trim().replace(/\/$/, '')
}

function getElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id)
  if (!element) {
    throw new Error(`Element #${id} not found.`)
  }
  return element as T
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    const entities: Record<string, string> = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    }
    return entities[char]
  })
}

function parseSseEvent(rawEvent: string): { event: string; data: string } | null {
  const lines = rawEvent.split('\n')
  let event = 'message'
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim()
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart())
    }
  }
  if (!dataLines.length) {
    return null
  }
  return { event, data: dataLines.join('\n') }
}

class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: { detail?: unknown } | null
  ) {
    super(`HTTP ${status}`)
  }
}
