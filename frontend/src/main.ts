import './styles.css'
import {
  appendThinkingSectionDelta,
  finalThinkingContent,
  parseSseEvent,
  shouldSubmitTextareaKey,
  shouldUseFinalAnswerFallback,
  type AgentThinkingSection,
  type AgentProgress,
  type TraceEvent
} from './agent-stream'
import { OidcAuthController, type AuthSnapshot } from './auth'
import { invocationBlockedReason, type ServiceHealth } from './service-status'

type AgentResponse = {
  content: string
  trace_id: string
  cited_evidence_ids: string[]
  approval_status: string
}

type AssetStatus =
  | 'uploading'
  | 'processing'
  | 'ready'
  | 'partial'
  | 'failed'
  | 'expired'
  | 'deleted'

type AssetResponse = {
  asset_id: string
  media_type: string
  size_bytes: number
  width: number
  height: number
  status: AssetStatus
  failure_code: string | null
  expires_at: string
}

type AssetAnalysisResponse = {
  analysis: {
    summary: string
  }
}

type PendingAttachment = {
  localId: string
  file: File
  previewUrl: string
  assetId?: string
  status: 'uploading' | AssetStatus
  analysisSummary?: string
  error?: string
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
  thinking?: string
  thinkingSections?: AgentThinkingSection[]
  thinkingStreaming?: boolean
  thinkingCollapsed?: boolean
  progress?: AgentProgress | null
  attachments?: Array<{ name: string; previewUrl: string; sizeLabel: string }>
}

type AgentStage =
  | 'idle'
  | 'preparing'
  | 'accepted'
  | 'attachment_loading'
  | 'image_analysis_loaded'
  | 'graph_completed'
  | 'thinking'
  | 'answering'
  | 'completed'
  | 'failed'

type RuntimeState = {
  tenantId: string
  userId: string
  sessionId: string
  messages: ChatMessage[]
  trace: TraceResponse | null
  health: ServiceHealth
  busy: boolean
  error: string
  lastLatencyMs: number | null
  attachments: PendingAttachment[]
  agentStage: AgentStage
  authStatus: AuthSnapshot['status']
  authDisplayName: string
}

const apiBaseUrl = normalizeBaseUrl(import.meta.env.VITE_AGENT_API_BASE_URL)
const localAccessToken = normalizeAccessToken(import.meta.env.VITE_AGENT_ACCESS_TOKEN)
let accessToken = localAccessToken
let authController: OidcAuthController | null = null
let authEnabled = false
const maxAttachments = 4
const maxImageBytes = 10 * 1024 * 1024

const state: RuntimeState = {
  tenantId: 'tenant_1',
  userId: 'user_1',
  sessionId: `session_${crypto.randomUUID().slice(0, 8)}`,
  messages: [],
  trace: null,
  health: 'checking',
  busy: false,
  error: '',
  lastLatencyMs: null,
  attachments: [],
  agentStage: 'idle',
  authStatus: 'loading',
  authDisplayName: ''
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
        <div class="system-card__row">
          <span>Agent stage</span>
          <strong id="agentStageValue">待命</strong>
        </div>
        <div class="system-card__row">
          <span>Identity</span>
          <strong id="identityValue">初始化中</strong>
        </div>
      </section>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div>
          <span class="eyebrow">LIVE AGENT LINK</span>
          <h2>对话与执行观测</h2>
        </div>
        <div class="topbar__actions">
          <button id="authButton" class="ghost-button" type="button" hidden>登录</button>
          <button id="refreshHealth" class="ghost-button" type="button">刷新状态</button>
        </div>
      </header>

      <div class="console-grid">
        <section class="chat-panel">
          <div id="messageList" class="message-list"></div>
          <form id="messageForm" class="composer">
            <div id="attachmentTray" class="attachment-tray"></div>
            <div class="composer__controls">
              <button id="attachButton" class="attach-button" type="button" title="添加图片" aria-label="添加图片">+</button>
              <input id="fileInput" class="visually-hidden" type="file" accept="image/jpeg,image/png,image/webp" multiple />
              <textarea id="messageInput" rows="3" placeholder="输入要交给 Agent 处理的问题"></textarea>
              <button id="sendButton" type="submit">发送</button>
            </div>
          </form>
        </section>

        <aside class="trace-panel">
          <div class="trace-panel__header">
            <div>
              <span class="eyebrow">TRACE</span>
              <h3 id="traceTitle">运行轨迹</h3>
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
const attachButton = getElement<HTMLButtonElement>('attachButton')
const fileInput = getElement<HTMLInputElement>('fileInput')
const refreshHealthButton = getElement<HTMLButtonElement>('refreshHealth')
const authButton = getElement<HTMLButtonElement>('authButton')

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
authButton.addEventListener('click', () => {
  void toggleAuthentication()
})
attachButton.addEventListener('click', () => fileInput.click())
fileInput.addEventListener('change', () => {
  void addSelectedImages(fileInput.files)
})
window.addEventListener('beforeunload', revokeAllPreviewUrls)
messageForm.addEventListener('submit', (event) => {
  event.preventDefault()
  void submitMessage()
})
messageInput.addEventListener('keydown', (event) => {
  if (!shouldSubmitTextareaKey(event)) {
    return
  }
  event.preventDefault()
  if (!state.busy) {
    messageForm.requestSubmit()
  }
})

render()
void checkHealth()
void initializeAuthentication()

async function initializeAuthentication() {
  try {
    authController = new OidcAuthController(
      window.__BS_AGENT_RUNTIME_CONFIG__ ?? {},
      applyAuthSnapshot
    )
    authEnabled = authController.enabled
    if (!authEnabled) {
      state.authStatus = 'disabled'
      state.authDisplayName = localAccessToken ? '本地令牌' : '本地调试身份'
      render()
    }
    await authController.initialize()
  } catch (error) {
    state.authStatus = 'error'
    state.error = error instanceof Error ? error.message : '认证初始化失败。'
    render()
  }
}

function applyAuthSnapshot(snapshot: AuthSnapshot) {
  state.authStatus = snapshot.status
  state.authDisplayName = snapshot.displayName
  if (snapshot.status === 'disabled') {
    accessToken = localAccessToken
  } else {
    accessToken = snapshot.accessToken
  }
  if (snapshot.status === 'authenticated') {
    state.tenantId = snapshot.tenantId
    state.userId = snapshot.userId
    if (!snapshot.tenantId || !snapshot.userId) {
      state.authStatus = 'error'
      state.error = '身份令牌缺少 tenant_id 或 sub claim，请联系管理员。'
    }
  }
  if (snapshot.error) {
    state.error = snapshot.error
  }
  render()
}

async function toggleAuthentication() {
  if (!authController?.enabled) {
    return
  }
  state.error = ''
  try {
    if (state.authStatus === 'authenticated') {
      await authController.logout()
    } else {
      await authController.login()
    }
  } catch (error) {
    state.error = error instanceof Error ? error.message : '认证操作失败。'
    render()
  }
}

async function submitMessage() {
  const message = messageInput.value.trim()
  state.error = ''
  if (!message) {
    state.error = '请输入消息。'
    render()
    return
  }
  const serviceBlockedReason = invocationBlockedReason(state.health)
  if (serviceBlockedReason) {
    state.error = serviceBlockedReason
    render()
    return
  }
  if (authEnabled && state.authStatus !== 'authenticated') {
    state.error = '请先登录后再使用 Agent。'
    render()
    return
  }
  if (!state.tenantId || !state.userId || !state.sessionId) {
    state.error = 'Tenant、User 和 Session 都不能为空。'
    render()
    return
  }
  const unavailableAttachment = state.attachments.find(
    (attachment) => !['ready', 'partial'].includes(attachment.status)
  )
  if (unavailableAttachment) {
    state.error = '请等待图片分析完成后再发送。'
    render()
    return
  }

  state.busy = true
  state.agentStage = 'preparing'
  state.lastLatencyMs = null
  const selectedAttachments = [...state.attachments]
  state.messages.push({
    role: 'user',
    content: message,
    attachments: selectedAttachments.map((attachment) => ({
      name: attachment.file.name,
      previewUrl: attachment.previewUrl,
      sizeLabel: formatBytes(attachment.file.size)
    }))
  })
  const agentMessage: ChatMessage = {
    role: 'agent',
    content: '',
    streaming: true,
    thinking: '',
    thinkingSections: [],
    thinkingStreaming: true,
    thinkingCollapsed: false,
    progress: {
      stage: 'planning',
      message: '正在规划分析范围'
    }
  }
  state.messages.push(agentMessage)
  messageInput.value = ''
  render()

  const startedAt = performance.now()
  const typewriter = createMessageTypewriter(agentMessage)
  const thinkingTypewriter = createThinkingTypewriter(agentMessage)
  let receivedAnswerDelta = false
  let rawThinkingContent = ''
  let thinkingCollapsedForAnswer = false
  const collapseThinkingPanel = () => {
    if (thinkingCollapsedForAnswer) {
      return
    }
    thinkingCollapsedForAnswer = true
    thinkingTypewriter.cancel()
    agentMessage.thinking = finalThinkingContent({
      thinking: rawThinkingContent || agentMessage.thinking || '',
      userMessage: message
    })
    agentMessage.thinkingStreaming = false
    agentMessage.thinkingCollapsed = true
    render()
  }
  try {
    const response = await streamAgentMessage(
      message,
      selectedAttachments.flatMap((attachment) =>
        attachment.assetId ? [attachment.assetId] : []
      ),
      {
      onThinkingDelta: (content, meta) => {
        rawThinkingContent += content
        if (!thinkingCollapsedForAnswer) {
          thinkingTypewriter.enqueue(content, meta)
        }
      },
      onProgress: (progress) => {
        agentMessage.progress = progress
        render()
      },
      onDelta: (content) => {
        agentMessage.progress = null
        receivedAnswerDelta = true
        collapseThinkingPanel()
        typewriter.enqueue(content)
      },
      onTraceEvents: (trace) => {
        state.trace = trace
        render()
      },
      onAgentEvent: (event) => {
        mergeTraceEvent(event)
        render()
      },
      onStatus: (stage) => {
        state.agentStage = stage
        if (stage === 'answering') {
          agentMessage.progress = null
          collapseThinkingPanel()
        }
        render()
      }
      }
    )
    if (
      shouldUseFinalAnswerFallback({
        receivedAnswerDelta,
        currentContent: agentMessage.content,
        finalContent: response.content
      })
    ) {
      typewriter.enqueue(response.content)
    }
    await thinkingTypewriter.flush()
    await typewriter.flush()
    state.lastLatencyMs = Math.round(performance.now() - startedAt)
    agentMessage.streaming = false
    agentMessage.progress = null
    collapseThinkingPanel()
    if (response.content && agentMessage.content !== response.content) {
      agentMessage.content = response.content
    }
    agentMessage.traceId = response.trace_id
    agentMessage.citedEvidenceIds = response.cited_evidence_ids
    agentMessage.approvalStatus = response.approval_status
    state.trace = await getTrace(response.trace_id)
    state.attachments = []
    state.agentStage = 'completed'
  } catch (error) {
    thinkingTypewriter.cancel()
    typewriter.cancel()
    agentMessage.streaming = false
    agentMessage.thinkingStreaming = false
    agentMessage.progress = null
    if (!agentMessage.content) {
      state.messages = state.messages.filter((item) => item !== agentMessage)
    }
    state.error = apiErrorMessage(error)
    state.agentStage = 'failed'
  } finally {
    state.busy = false
    render()
  }
}

function createMessageTypewriter(message: ChatMessage) {
  let queue = ''
  let timer: number | null = null
  let active = true
  const idleResolvers: Array<() => void> = []
  const intervalMs = 24

  const resolveIdle = () => {
    if (queue || timer !== null) {
      return
    }
    while (idleResolvers.length) {
      idleResolvers.shift()?.()
    }
  }

  const schedule = () => {
    if (!active || timer !== null || !queue) {
      return
    }
    timer = window.setTimeout(() => {
      timer = null
      const chunkSize = queue.length > 120 ? 5 : queue.length > 48 ? 3 : 2
      message.content += queue.slice(0, chunkSize)
      queue = queue.slice(chunkSize)
      render()
      schedule()
      resolveIdle()
    }, intervalMs)
  }

  return {
    enqueue(content: string) {
      if (!active || !content) {
        return
      }
      queue += content
      schedule()
    },
    flush() {
      if (!queue && timer === null) {
        return Promise.resolve()
      }
      return new Promise<void>((resolve) => {
        idleResolvers.push(resolve)
        schedule()
      })
    },
    cancel() {
      active = false
      queue = ''
      if (timer !== null) {
        window.clearTimeout(timer)
        timer = null
      }
      resolveIdle()
    }
  }
}

function createThinkingTypewriter(message: ChatMessage) {
  let queue: Array<{ content: string; stage: string; label: string }> = []
  let timer: number | null = null
  let active = true
  const idleResolvers: Array<() => void> = []
  const intervalMs = 18

  const resolveIdle = () => {
    if (queue.length || timer !== null) {
      return
    }
    while (idleResolvers.length) {
      idleResolvers.shift()?.()
    }
  }

  const schedule = () => {
    if (!active || timer !== null || !queue.length) {
      return
    }
    timer = window.setTimeout(() => {
      timer = null
      const head = queue[0]
      const chunkSize = head.content.length > 100 ? 5 : head.content.length > 40 ? 3 : 2
      const chunk = head.content.slice(0, chunkSize)
      message.thinking = `${message.thinking ?? ''}${chunk}`
      if (!message.thinkingSections) {
        message.thinkingSections = []
      }
      appendThinkingSectionDelta(message.thinkingSections, {
        stage: head.stage,
        label: head.label,
        content: chunk
      })
      head.content = head.content.slice(chunkSize)
      if (!head.content) {
        queue.shift()
      }
      render()
      schedule()
      resolveIdle()
    }, intervalMs)
  }

  return {
    enqueue(content: string, meta?: { stage?: string; label?: string }) {
      if (!active || !content) {
        return
      }
      message.thinkingCollapsed = false
      queue.push({
        content,
        stage: meta?.stage ?? 'analysis',
        label: meta?.label ?? '分析'
      })
      schedule()
    },
    flush() {
      if (!queue.length && timer === null) {
        return Promise.resolve()
      }
      return new Promise<void>((resolve) => {
        idleResolvers.push(resolve)
        schedule()
      })
    },
    cancel() {
      active = false
      queue = []
      if (timer !== null) {
        window.clearTimeout(timer)
        timer = null
      }
      resolveIdle()
    }
  }
}

async function streamAgentMessage(
  message: string,
  attachmentIds: string[],
  handlers: {
    onThinkingDelta: (
      content: string,
      meta: { stage?: string; label?: string }
    ) => void
    onProgress: (progress: AgentProgress) => void
    onDelta: (content: string) => void
    onTraceEvents: (trace: TraceResponse) => void
    onAgentEvent: (event: TraceEvent) => void
    onStatus: (stage: AgentStage) => void
  }
): Promise<AgentResponse> {
  const response = await fetch(
    `${apiBaseUrl}/api/sessions/${encodeURIComponent(state.sessionId)}/messages/stream`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...authHeaders()
      },
      body: JSON.stringify({
        message,
        attachments: attachmentIds
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
      if (parsed.event === 'thinking_delta') {
        const payload = JSON.parse(parsed.data) as {
          stage?: string
          label?: string
          content?: string
        }
        if (payload.content) {
          handlers.onThinkingDelta(payload.content, {
            stage: payload.stage,
            label: payload.label
          })
        }
      }
      if (parsed.event === 'progress') {
        const payload = JSON.parse(parsed.data) as AgentProgress
        if (payload.stage && payload.message) {
          handlers.onProgress(payload)
        }
      }
      if (parsed.event === 'status') {
        const payload = JSON.parse(parsed.data) as { stage?: AgentStage }
        if (payload.stage) {
          handlers.onStatus(payload.stage)
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
      if (parsed.event === 'agent_event') {
        handlers.onAgentEvent(JSON.parse(parsed.data) as TraceEvent)
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
        throw new AgentStreamError(
          payload.code ?? 'agent_stream_failed',
          payload.message ?? 'Agent 执行失败。'
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

function mergeTraceEvent(event: TraceEvent) {
  if (!state.trace || state.trace.trace_id !== event.trace_id) {
    state.trace = {
      trace_id: event.trace_id,
      events: [],
      artifacts: {}
    }
  }
  const eventKey = traceEventKey(event)
  const existingIndex = state.trace.events.findIndex(
    (item) => traceEventKey(item) === eventKey
  )
  if (existingIndex >= 0) {
    state.trace.events[existingIndex] = event
    return
  }
  state.trace.events.push(event)
}

function traceEventKey(event: TraceEvent): string {
  return event.event_id ?? `${event.trace_id}:${event.node_name}:${event.status}:${event.latency_ms}`
}

async function getTrace(traceId: string): Promise<TraceResponse> {
  const response = await fetch(`${apiBaseUrl}/api/traces/${encodeURIComponent(traceId)}`, {
    headers: authHeaders()
  })
  return parseJsonResponse<TraceResponse>(response)
}

async function checkHealth() {
  state.health = 'checking'
  render()
  try {
    const response = await fetch(`${apiBaseUrl}/health/ready`)
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
  const authenticationRequired = authEnabled && state.authStatus !== 'authenticated'
  tenantInput.disabled = authEnabled
  userInput.disabled = authEnabled
  sendButton.disabled = state.busy || authenticationRequired
  attachButton.disabled =
    state.busy || authenticationRequired || state.attachments.length >= maxAttachments
  sendButton.textContent = state.busy ? '运行中' : '发送'
  authButton.hidden = !authEnabled
  authButton.disabled = state.authStatus === 'loading'
  authButton.textContent = state.authStatus === 'authenticated' ? '退出登录' : '登录'

  renderHealth()
  renderAttachments()
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
  getElement<HTMLElement>('agentStageValue').textContent = agentStageLabel(state.agentStage)
  getElement<HTMLElement>('identityValue').textContent = authStatusLabel()
}

function authStatusLabel(): string {
  if (state.authStatus === 'authenticated') {
    return state.authDisplayName || state.userId
  }
  return {
    disabled: state.authDisplayName || '本地调试身份',
    loading: '初始化中',
    anonymous: '未登录',
    error: '认证异常'
  }[state.authStatus]
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
      const attachments = message.attachments?.length
        ? `<div class="message__attachments">${message.attachments
            .map(
              (attachment) => `
                <figure>
                  <img src="${escapeHtml(attachment.previewUrl)}" alt="${escapeHtml(attachment.name)}" />
                  <figcaption>${escapeHtml(attachment.name)} · ${escapeHtml(attachment.sizeLabel)}</figcaption>
                </figure>
              `
            )
            .join('')}</div>`
        : ''
      const thinkingPanel =
        message.role === 'agent' && (message.thinking || message.thinkingStreaming)
          ? `<details class="message__thinking" ${message.thinkingCollapsed ? '' : 'open'}>
              <summary>
                <span>分析思路</span>
                <small>${message.thinkingStreaming ? '生成中' : '已收起'}</small>
              </summary>
              ${renderThinkingSections(message)}
            </details>`
          : ''
      const progress =
        message.role === 'agent' && message.progress
          ? `<div class="message__progress" role="status">
              <span aria-hidden="true"></span>
              ${escapeHtml(message.progress.message)}
            </div>`
          : ''
      return `
        <article class="message message--${message.role}">
          <div class="message__role">${message.role === 'user' ? 'USER' : 'AGENT'}</div>
          ${thinkingPanel}
          ${progress}
          <p>${escapeHtml(message.content)}${stream}</p>
          ${attachments}
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

function renderThinkingSections(message: ChatMessage): string {
  const sections = (message.thinkingSections ?? []).filter((section) =>
    section.content.trim()
  )
  if (!sections.length) {
    return `<p>${escapeHtml(message.thinking || '正在拆解问题与确认可用证据...')}</p>`
  }
  return `
    <ol class="message__thinking-steps">
      ${sections
        .map(
          (section) => `
            <li>
              <strong>${escapeHtml(section.label)}</strong>
              <p>${escapeHtml(section.content.trim())}</p>
            </li>
          `
        )
        .join('')}
    </ol>
  `
}

function renderAttachments() {
  const tray = getElement<HTMLDivElement>('attachmentTray')
  tray.innerHTML = state.attachments
    .map(
      (attachment) => `
        <article class="attachment-item attachment-item--${attachment.status}">
          <img src="${escapeHtml(attachment.previewUrl)}" alt="${escapeHtml(attachment.file.name)}" />
          <div>
            <strong>${escapeHtml(attachment.file.name)}</strong>
            <span>${escapeHtml(formatBytes(attachment.file.size))} · ${escapeHtml(attachmentStatusLabel(attachment))}</span>
            ${attachment.analysisSummary ? `<small>${escapeHtml(attachment.analysisSummary)}</small>` : ''}
          </div>
          <button type="button" data-remove-attachment="${escapeHtml(attachment.localId)}" title="移除图片" aria-label="移除图片">×</button>
        </article>
      `
    )
    .join('')
  tray.hidden = state.attachments.length === 0
  tray.querySelectorAll<HTMLButtonElement>('[data-remove-attachment]').forEach((button) => {
    button.addEventListener('click', () => {
      removeAttachment(button.dataset.removeAttachment ?? '')
    })
  })
}

async function addSelectedImages(files: FileList | null) {
  const selected = Array.from(files ?? [])
  fileInput.value = ''
  state.error = ''
  if (state.attachments.length + selected.length > maxAttachments) {
    state.error = `每次最多添加 ${maxAttachments} 张图片。`
    render()
    return
  }
  const invalid = selected.find(
    (file) =>
      !['image/jpeg', 'image/png', 'image/webp'].includes(file.type) ||
      file.size > maxImageBytes
  )
  if (invalid) {
    state.error = `${invalid.name} 的格式不受支持或超过 10 MB。`
    render()
    return
  }
  const pending = selected.map<PendingAttachment>((file) => ({
    localId: crypto.randomUUID(),
    file,
    previewUrl: URL.createObjectURL(file),
    status: 'uploading'
  }))
  state.attachments.push(...pending)
  render()
  await Promise.all(pending.map(uploadAndTrackAttachment))
}

async function uploadAndTrackAttachment(attachment: PendingAttachment) {
  const form = new FormData()
  form.append('file', attachment.file)
  form.append('session_id', state.sessionId)
  try {
    const response = await fetch(`${apiBaseUrl}/api/assets/images`, {
      method: 'POST',
      headers: authHeaders(),
      body: form
    })
    const asset = await parseJsonResponse<AssetResponse>(response)
    attachment.assetId = asset.asset_id
    attachment.status = asset.status
    render()
    await pollAttachment(attachment)
  } catch (error) {
    attachment.status = 'failed'
    attachment.error = apiErrorMessage(error)
    render()
  }
}

async function pollAttachment(attachment: PendingAttachment) {
  if (!attachment.assetId) {
    return
  }
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (!state.attachments.includes(attachment)) {
      return
    }
    const response = await fetch(
      `${apiBaseUrl}/api/assets/${encodeURIComponent(attachment.assetId)}`,
      { headers: authHeaders() }
    )
    const asset = await parseJsonResponse<AssetResponse>(response)
    attachment.status = asset.status
    attachment.error = asset.failure_code ?? undefined
    if (asset.status === 'ready' || asset.status === 'partial') {
      const analysisResponse = await fetch(
        `${apiBaseUrl}/api/assets/${encodeURIComponent(attachment.assetId)}/analysis`,
        { headers: authHeaders() }
      )
      const analysis = await parseJsonResponse<AssetAnalysisResponse>(analysisResponse)
      attachment.analysisSummary = analysis.analysis.summary
      render()
      return
    }
    if (['failed', 'expired', 'deleted'].includes(asset.status)) {
      render()
      return
    }
    render()
    await delay(1500)
  }
  attachment.status = 'failed'
  attachment.error = 'analysis_timeout'
  render()
}

function removeAttachment(localId: string) {
  const attachment = state.attachments.find((item) => item.localId === localId)
  if (!attachment) {
    return
  }
  state.attachments = state.attachments.filter((item) => item !== attachment)
  if (!isPreviewUsedByMessage(attachment.previewUrl)) {
    URL.revokeObjectURL(attachment.previewUrl)
  }
  if (attachment.assetId) {
    void fetch(`${apiBaseUrl}/api/assets/${encodeURIComponent(attachment.assetId)}`, {
      method: 'DELETE',
      headers: authHeaders()
    })
  }
  render()
}

function isPreviewUsedByMessage(previewUrl: string): boolean {
  return state.messages.some((message) =>
    message.attachments?.some((attachment) => attachment.previewUrl === previewUrl)
  )
}

function revokeAllPreviewUrls() {
  const urls = new Set([
    ...state.attachments.map((attachment) => attachment.previewUrl),
    ...state.messages.flatMap((message) =>
      message.attachments?.map((attachment) => attachment.previewUrl) ?? []
    )
  ])
  urls.forEach((url) => URL.revokeObjectURL(url))
}

function attachmentStatusLabel(attachment: PendingAttachment): string {
  if (attachment.error) {
    return attachmentErrorMessage(attachment.error)
  }
  return {
    uploading: '正在安全上传',
    processing: '正在识别与分析',
    ready: '分析完成',
    partial: '部分识别完成',
    failed: '分析失败',
    expired: '已过期',
    deleted: '已删除'
  }[attachment.status]
}

function agentStageLabel(stage: AgentStage): string {
  return {
    idle: '待命',
    preparing: '准备请求',
    accepted: '请求已受理',
    attachment_loading: '加载图片摘要',
    image_analysis_loaded: '图片摘要已加载',
    graph_completed: '推理链路完成',
    thinking: '生成分析思路',
    answering: '生成最终回答',
    completed: '回答完成',
    failed: '执行失败'
  }[stage]
}

function attachmentErrorMessage(code: string): string {
  const messages: Record<string, string> = {
    analysis_dispatch_failed: '分析任务派发失败，请重新上传',
    analysis_timeout: '分析超时，请稍后重新上传',
    image_analysis_unavailable: '图片识别服务暂不可用',
    asset_object_missing: '图片文件不可用，请重新上传',
    vision_persistence_failed: '分析结果保存失败，请重试'
  }
  if (code.startsWith('vision_')) {
    return messages[code] ?? '视觉分析暂不可用，系统可能仅返回 OCR 结果'
  }
  if (code.startsWith('ocr_')) {
    return messages[code] ?? '文字识别暂不可用，系统可能仅返回视觉分析结果'
  }
  return messages[code] ?? '图片分析失败，请移除后重新上传'
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function authHeaders(): Record<string, string> {
  if (accessToken) {
    return { Authorization: `Bearer ${accessToken}` }
  }
  if (authEnabled) {
    return {}
  }
  return {
    'X-Tenant-ID': state.tenantId,
    'X-User-ID': state.userId
  }
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

function renderTrace() {
  const traceTitle = getElement<HTMLHeadingElement>('traceTitle')
  const timeline = getElement<HTMLDivElement>('traceTimeline')
  const artifactPreview = getElement<HTMLPreElement>('artifactPreview')

  if (!state.trace) {
    traceTitle.textContent = '运行轨迹'
    timeline.innerHTML = '<div class="empty-state">节点事件会在 Agent 执行时显示。</div>'
    artifactPreview.textContent = ''
    return
  }

  traceTitle.textContent = `运行轨迹 · ${state.trace.trace_id}`
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
  if (error instanceof AgentStreamError) {
    return userFacingError(error.code, error.message)
  }
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return '登录已过期，请重新登录。'
    }
    const detail = error.body?.detail
    if (typeof detail === 'string') {
      return detail
    }
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const code = 'code' in detail ? String(detail.code) : `http_${error.status}`
      return userFacingError(code, String(detail.message))
    }
    if (detail && typeof detail === 'object' && 'code' in detail) {
      return `${String(detail.code)} · HTTP ${error.status}`
    }
    return `请求失败 · HTTP ${error.status}`
  }
  return error instanceof Error ? error.message : '请求失败'
}

function userFacingError(code: string, fallback: string): string {
  const messages: Record<string, string> = {
    asset_not_found: '图片不存在或无权访问，请重新上传。',
    asset_not_ready: '图片仍在识别中，请稍后再发送。',
    asset_analysis_failed: '图片分析失败，请移除后重新上传。',
    asset_too_large: '图片超过 10 MB，请压缩后重新上传。',
    unsupported_image_type: '仅支持 JPEG、PNG 和 WEBP 图片。',
    attachment_resolver_unavailable: '图片摘要服务暂不可用，请稍后重试。',
    intent_classifier_failed: '意图识别服务暂不可用，请稍后重试。',
    llm_configuration_error:
      'LLM Provider 配置不完整，请设置 BS_AGENT_DEEPSEEK_API_KEY 后重启本地服务。'
  }
  return messages[code] ?? fallback
}

function normalizeBaseUrl(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) {
    return ''
  }
  return value.trim().replace(/\/$/, '')
}

function normalizeAccessToken(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
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

class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: { detail?: unknown } | null
  ) {
    super(`HTTP ${status}`)
  }
}

class AgentStreamError extends Error {
  constructor(
    readonly code: string,
    message: string
  ) {
    super(message)
  }
}
