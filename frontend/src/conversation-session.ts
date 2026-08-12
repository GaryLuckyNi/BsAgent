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
      return {
        role: 'agent',
        content: item.content,
        ...(traceId ? { traceId } : {}),
        ...(approvalStatus ? { approvalStatus } : {}),
        ...(citedEvidenceIds ? { citedEvidenceIds } : {})
      }
    })
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
