export type ConversationSessionStorage = Pick<Storage, 'getItem' | 'setItem'>

export type ConversationIdentity = {
  tenantId: string
  userId: string
}

export type PersistedConversationMessage = {
  message_id?: string
  run_id?: string
  role: 'user' | 'assistant'
  status: string
  content: string
  sequence_no: number
  metadata: Record<string, unknown>
}

export type RestoredConversationMessage = {
  messageId?: string
  runId?: string
  status?: string
  role: 'user' | 'agent'
  content: string
  traceId?: string
  citedEvidenceIds?: string[]
  approvalStatus?: string
  researchProcess?: RestoredResearchProcessEvent[]
}

export type RestoredResearchProcessEvent = {
  id: string
  kind: 'activity' | 'summary'
  label: string
  message: string
  status: 'started' | 'completed' | 'failed'
  sequence: number
  activityType?: 'web_search' | 'market_data' | 'evidence'
  groupId?: string
  taskId?: string
  taskLabel?: string
  query?: string
  resultCount?: number
  resultUrls?: string[]
}

export function resolveConversationSession(
  storage: ConversationSessionStorage,
  identity: ConversationIdentity,
  createSessionId: () => string
): string {
  const key = conversationSessionStorageKey(identity)
  const saved = storage.getItem(key)?.trim()
  if (saved) {
    return saved
  }
  const sessionId = createSessionId()
  storage.setItem(key, sessionId)
  return sessionId
}

export function saveConversationSession(
  storage: ConversationSessionStorage,
  identity: ConversationIdentity,
  sessionId: string
): void {
  storage.setItem(conversationSessionStorageKey(identity), sessionId)
}

export function conversationIdentityKey(identity: ConversationIdentity): string {
  return `${identity.tenantId}\u0000${identity.userId}`
}

export function toConversationMessages(
  items: PersistedConversationMessage[]
): RestoredConversationMessage[] {
  return [...items]
    .sort((left, right) => left.sequence_no - right.sequence_no)
    .filter((item) => item.content.trim() || (item.role === 'assistant' && item.status !== 'completed'))
    .map((item) => {
      const identity = item.message_id ? {
        messageId: item.message_id,
        runId: item.run_id,
        status: item.status
      } : {}
      if (item.role === 'user') {
        return { role: 'user', content: item.content, ...identity }
      }
      const traceId = stringMetadata(item.metadata, 'trace_id')
      const approvalStatus = stringMetadata(item.metadata, 'approval_status')
      const citedEvidenceIds = stringArrayMetadata(item.metadata, 'cited_evidence_ids')
      const researchProcess = researchProcessMetadata(item.metadata)
      return {
        role: 'agent',
        content: item.content,
        ...identity,
        ...(traceId ? { traceId } : {}),
        ...(approvalStatus ? { approvalStatus } : {}),
        ...(citedEvidenceIds ? { citedEvidenceIds } : {}),
        ...(researchProcess ? { researchProcess } : {})
      }
    })
}

function researchProcessMetadata(
  metadata: Record<string, unknown>
): RestoredResearchProcessEvent[] | undefined {
  const value = metadata.research_process
  if (!Array.isArray(value)) return undefined
  const events = value.flatMap((item): RestoredResearchProcessEvent[] => {
    if (!item || typeof item !== 'object') return []
    const event = item as Record<string, unknown>
    if (
      typeof event.id !== 'string'
      || (event.kind !== 'activity' && event.kind !== 'summary')
      || typeof event.message !== 'string'
    ) return []
    const activityType = (
      event.activity_type === 'web_search'
      || event.activity_type === 'market_data'
      || event.activity_type === 'evidence'
    ) ? event.activity_type : undefined
    return [{
      id: event.id,
      kind: event.kind,
      label: typeof event.label === 'string' ? event.label : '研究过程',
      message: event.message,
      status: event.status === 'failed' || event.status === 'completed' ? event.status : 'started',
      sequence: typeof event.sequence === 'number' ? event.sequence : 0,
      ...(activityType ? { activityType } : {}),
      ...stringEventField(event, 'group_id', 'groupId'),
      ...stringEventField(event, 'task_id', 'taskId'),
      ...stringEventField(event, 'task_label', 'taskLabel'),
      ...stringEventField(event, 'query', 'query'),
      ...(typeof event.result_count === 'number' ? { resultCount: event.result_count } : {}),
      ...(Array.isArray(event.result_urls) ? {
        resultUrls: event.result_urls.filter((url): url is string => typeof url === 'string')
      } : {})
    }]
  })
  return events.length ? events : undefined
}

function stringEventField(
  event: Record<string, unknown>,
  source: string,
  target: string
): Record<string, string> {
  const value = event[source]
  return typeof value === 'string' && value ? { [target]: value } : {}
}

function conversationSessionStorageKey(identity: ConversationIdentity): string {
  return [
    'bs-agent',
    'conversation',
    encodeURIComponent(identity.tenantId),
    encodeURIComponent(identity.userId)
  ].join(':')
}

function stringMetadata(metadata: Record<string, unknown>, key: string): string | undefined {
  const value = metadata[key]
  return typeof value === 'string' && value ? value : undefined
}

function stringArrayMetadata(
  metadata: Record<string, unknown>,
  key: string
): string[] | undefined {
  const value = metadata[key]
  if (!Array.isArray(value)) {
    return undefined
  }
  const strings = value.filter((item): item is string => typeof item === 'string')
  return strings.length ? strings : undefined
}
