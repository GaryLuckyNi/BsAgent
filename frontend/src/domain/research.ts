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
  status?: 'streaming' | 'complete' | 'error'
  thinking?: string
  attachments?: ResearchAttachmentSnapshot[]
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
