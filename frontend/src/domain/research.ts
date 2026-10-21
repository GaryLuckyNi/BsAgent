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

export type ResearchUserInput = {
  interruptId: string
  interactionId: string
  inputType: 'selection'
  question: string
  options: ResearchUserInputOption[]
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
