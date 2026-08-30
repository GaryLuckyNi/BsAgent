import * as Dialog from '@radix-ui/react-dialog'
import { ArrowUpRight, BookOpenText, X } from 'lucide-react'
import { useEffect, useRef } from 'react'

import type { EvidenceSource } from '../../domain/research'
import { cn } from '@/utils/cn'

export function EvidenceDrawer({ open, activeIndex, evidence, onOpenChange }: {
  open: boolean
  activeIndex: number | null
  evidence: EvidenceSource[]
  onOpenChange: (open: boolean) => void
}) {
  const activeRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (open) window.setTimeout(() => activeRef.current?.scrollIntoView({ block: 'center' }), 80)
  }, [activeIndex, open])
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay" />
        <Dialog.Content className="evidence-drawer">
          <header className="drawer-header">
            <div><span><BookOpenText size={15} /> 研究证据</span><Dialog.Title>证据来源</Dialog.Title></div>
            <Dialog.Close className="drawer-close" aria-label="关闭"><X size={20} /></Dialog.Close>
          </header>
          <Dialog.Description className="drawer-description">回答中的数字与下方资料顺序一一对应。</Dialog.Description>
          <div className="evidence-list">{evidence.map((source) => (
            <article
              key={source.id}
              ref={source.index === activeIndex ? activeRef : undefined}
              className={cn('evidence-card', source.index === activeIndex && 'evidence-card--active')}
            >
              <div className="evidence-card__meta"><b>{source.index}</b><span>{source.publisher ?? '研究资料'}</span><time>{source.publishedAt}</time></div>
              <h3>{source.title}</h3>
              {source.summary && <p>{source.summary}</p>}
              {source.url !== '#' && <a href={source.url} target="_blank" rel="noreferrer">打开原始资料 <ArrowUpRight size={14} /></a>}
            </article>
          ))}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
