export type ConversationSessionStorage = Pick<Storage, 'getItem' | 'setItem'>

export type ConversationIdentity = {
  tenantId: string
  userId: string
}

export type PersistedConversationMessage = {
  role: 'user' | 'assistant'
  status: string
  content: string
  sequence_no: number
  metadata: Record<string, unknown>
}

export type RestoredConversationMessage = {
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
    .filter((item) => item.content.trim())
    .map((item) => {
      if (item.role === 'user') {
        return { role: 'user', content: item.content }
      }
      const traceId = stringMetadata(item.metadata, 'trace_id')
      const approvalStatus = stringMetadata(item.metadata, 'approval_status')
      const citedEvidenceIds = stringArrayMetadata(item.metadata, 'cited_evidence_ids')
      const researchProcess = researchProcessMetadata(item.metadata)
      return {
        role: 'agent',
        content: item.content,
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
    return [{
      id: event.id,
      kind: event.kind,
      label: typeof event.label === 'string' ? event.label : '研究过程',
      message: event.message,
      status: event.status === 'failed' || event.status === 'completed' ? event.status : 'started',
      sequence: typeof event.sequence === 'number' ? event.sequence : 0
    }]
  })
  return events.length ? events : undefined
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
