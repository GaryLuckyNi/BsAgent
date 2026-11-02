import type { EvidenceSource } from '@/domain/research'
import { fromMarkdown } from 'mdast-util-from-markdown'

export function webSourceUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || /[\u0000-\u001f\u007f]/.test(value)) return
  try {
    const url = new URL(value)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return
    return url.href
  } catch { return }
}

export function parseResearchSources(value: unknown): EvidenceSource[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<number>()
  return value.slice(0, 200).flatMap((item): EvidenceSource[] => {
    if (!item || typeof item !== 'object') return []
    const source = item as Record<string, unknown>
    const url = webSourceUrl(source.url)
    if (!url || typeof source.source_id !== 'string' || !/^source_[1-9]\d*$/.test(source.source_id)) return []
    const index = Number(source.source_id.slice(7))
    if (!Number.isSafeInteger(index) || source.index !== index || seen.has(index)) return []
    seen.add(index)
    return [{ id: source.source_id, index, url,
      title: typeof source.title === 'string' ? source.title.slice(0, 500) : '',
      ...(typeof source.snippet === 'string' && source.snippet.trim() ? { summary: source.snippet.slice(0, 2000) } : {}),
      ...(typeof source.published_at === 'string' && source.published_at.trim() ? { publishedAt: source.published_at.slice(0, 100) } : {})
    }]
  })
}

export type ResearchSourceGroup = EvidenceSource & { indices: number[]; cited: boolean; hostname: string }

function sourceUrlKey(value: unknown): string | undefined {
  const safe = webSourceUrl(value)
  if (!safe) return
  const url = new URL(safe)
  url.hash = ''
  return url.href
}

type MarkdownNode = { type: string; children?: MarkdownNode[]; value?: string; url?: string; identifier?: string }

export function groupResearchSources(sources: EvidenceSource[], content: string): ResearchSourceGroup[] {
  const tree = fromMarkdown(content)
  const definitions = new Map<string, string>()
  const references = new Set<string>()
  function visit(node: MarkdownNode, callback: (node: MarkdownNode) => void) {
    callback(node)
    node.children?.forEach((child) => visit(child, callback))
  }
  visit(tree, (node) => {
    if (node.type === 'definition' && node.identifier && node.url && !definitions.has(node.identifier)) definitions.set(node.identifier, node.url)
  })
  visit(tree, (node) => {
    if (node.type !== 'link' && node.type !== 'linkReference') return
    if (!node.children?.every((child) => child.type === 'text')) return
    const label = node.children.map((child) => child.value || '').join('')
    const url = sourceUrlKey(node.url || definitions.get(node.identifier || ''))
    if (url && /^[1-9]\d*$/.test(label)) references.add(`${label}\0${url}`)
  })
  const groups = new Map<string, ResearchSourceGroup>()
  for (const source of sources) {
    const key = sourceUrlKey(source.url)
    if (!key) continue
    const cited = references.has(`${source.index}\0${key}`)
    const group = groups.get(key)
    if (group) {
      if (!group.indices.includes(source.index)) group.indices.push(source.index)
      group.cited ||= cited
      group.title ||= source.title
      group.summary ||= source.summary
      group.publishedAt ||= source.publishedAt
      group.publisher ||= source.publisher
    } else {
      groups.set(key, { ...source, url: webSourceUrl(source.url)!, indices: [source.index], cited,
        hostname: new URL(key).hostname.replace(/^www\./, '') })
    }
  }
  return [...groups.values()]
}
