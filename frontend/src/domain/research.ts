export type EvidenceSource = {
  id: string
  index: number
  title: string
  url: string
  publisher?: string
  publishedAt?: string
  summary?: string
}

export type EvidenceInput = Omit<EvidenceSource, 'index'>

export type ResearchMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: string
  citations?: number[]
  evidence?: EvidenceSource[]
  status?: 'streaming' | 'stopping' | 'cancelled' | 'interrupted' | 'complete' | 'error'
  thinking?: string
  reasoning?: ResearchReasoning[]
  progress?: ResearchProgress[]
  processStatus?: string
  attachments?: ResearchAttachmentSnapshot[]
  userInput?: ResearchUserInput
}

export type ResearchUserInputOption = {
  value: string
  label: string
  description?: string | null
}

export type RecoveryItemView = {
  item_id: string
  task_goal: string
  original_tool: string
  reason: string
  source_notice: string
}

export type RecoveryBatchView = {
  version: 'permission-recovery-batch-v1'
  batch_id: string
  digest: string
  items: RecoveryItemView[]
}

export function parseRecoveryBatch(value: unknown): RecoveryBatchView | null {
  const record = (input: unknown): input is Record<string, unknown> =>
    typeof input === 'object' && input !== null && !Array.isArray(input)
  const text = (input: unknown, max: number): input is string =>
    typeof input === 'string' && input.trim().length > 0 && input.length <= max
  if (!record(value) || value.version !== 'permission-recovery-batch-v1'
      || !text(value.batch_id, 128) || typeof value.digest !== 'string'
      || !/^[a-f0-9]{64}$/.test(value.digest) || !Array.isArray(value.items)
      || value.items.length < 1 || value.items.length > 12) return null
  const ids = new Set<string>()
  for (const item of value.items) {
    if (!record(item) || !text(item.item_id, 128) || ids.has(item.item_id)
        || !text(item.task_goal, 500) || !text(item.original_tool, 255)
        || !text(item.reason, 240) || !text(item.source_notice, 240)) return null
    ids.add(item.item_id)
  }
  return value as RecoveryBatchView
}

export type ResearchUserInput = {
  interruptId: string
  interactionId: string
  inputType: 'selection'
  question: string
  options: ResearchUserInputOption[]
  recoveryBatch?: RecoveryBatchView
  presentationError?: string
  isSubmitting?: boolean
  selectedValue?: string
}

export type ResearchReasoning = {
  sequence?: number
  id: string
  label: string
  content: string
}

export type ResearchProgress = {
  sequence?: number
  id: string
  kind?: 'activity' | 'summary'
  stage?: string
  label: string
  message: string
  status: 'started' | 'completed' | 'failed'
  activityType?: 'web_search' | 'market_data' | 'evidence'
  groupId?: string
  queryCount?: number
  resultCount?: number
  queries?: ResearchSearchQuery[]
}

export type ResearchSearchQuery = {
  taskId: string
  query: string
  status: 'started' | 'completed' | 'failed'
  resultUrls: string[]
}

export type ResearchAttachmentSnapshot = {
  name: string
  previewUrl: string
  sizeLabel: string
}

export type ResearchThread = {
  id: string
  title: string
  updatedAt: string
  preview: string
  pinned?: boolean
}

export function buildEvidenceIndex(items: EvidenceInput[]): EvidenceSource[] {
  return items.map((item, position) => ({ ...item, index: position + 1 }))
}

export function resolveCitationNumber(
  evidence: EvidenceSource[],
  evidenceId: string
): number | undefined {
  return evidence.find((item) => item.id === evidenceId)?.index
}
