import { Children, createContext, useContext, useEffect, useRef, useState, type ComponentProps } from 'react'
import { ExternalLink } from 'lucide-react'
import { Popover } from 'radix-ui'

import { MessageResponse, type MessageResponseProps } from '@/components/ai-elements/message'
import type { EvidenceSource } from '@/domain/research'
import { webSourceUrl } from '@/services/research-sources'
import { SiteIcon } from './site-icon'

const EMPTY_SOURCES: EvidenceSource[] = []
const SourcesContext = createContext<EvidenceSource[]>(EMPTY_SOURCES)

function CitationLink({ href, children, node, ...props }: ComponentProps<'a'> & { node?: unknown }) {
  void node
  const sources = useContext(SourcesContext)
  const label = Children.toArray(children).map((child) => typeof child === 'string' || typeof child === 'number' ? String(child) : '').join('')
  const url = webSourceUrl(href)
  const isCitation = !!url && /^[1-9]\d*$/.test(label)
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const contentRef = useRef<HTMLDivElement>(null)
  const originalLinkRef = useRef<HTMLAnchorElement>(null)
  const citationRef = useRef<HTMLAnchorElement>(null)
  const returningFocus = useRef(false)
  useEffect(() => () => clearTimeout(timer.current), [])
  const cancelTimer = () => clearTimeout(timer.current)
  const schedule = (value: boolean, delay: number) => {
    cancelTimer()
    timer.current = setTimeout(() => setOpen(value), delay)
  }
  if (!isCitation || !url) return <a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>
  const parsed = new URL(url)
  const source = sources.find((item) => item.index === Number(label) && webSourceUrl(item.url) === url)
  const date = source?.publishedAt?.replace(/^(\d{4})-(\d{2})-(\d{2}).*$/, '$1/$2/$3')
  return <Popover.Root open={open} onOpenChange={setOpen} modal={false}>
    <Popover.Anchor asChild>
      <a {...props} ref={citationRef} href={url} className="research-citation" target="_blank" rel="noopener noreferrer"
        aria-label={`引用来源 ${label}`} data-active={open || undefined}
        onPointerEnter={() => schedule(true, 200)} onPointerLeave={() => schedule(false, 220)}
        onFocus={() => { cancelTimer(); if (returningFocus.current) { returningFocus.current = false; return } setOpen(true) }}
        onBlur={(event) => { if (!contentRef.current?.contains(event.relatedTarget)) schedule(false, 220) }}
        onKeyDown={(event) => {
          if (event.key === 'Escape') { cancelTimer(); setOpen(false) }
          if (event.key === 'ArrowDown') { event.preventDefault(); cancelTimer(); setOpen(true); requestAnimationFrame(() => originalLinkRef.current?.focus()) }
        }}
        onClick={(event) => { if (window.matchMedia?.('(hover: none)').matches) { event.preventDefault(); cancelTimer(); setOpen(true) } }}>
        {children}
      </a>
    </Popover.Anchor>
    <Popover.Portal>
      <Popover.Content ref={contentRef} className="citation-source-card" side="top" align="start" sideOffset={12}
        collisionPadding={16} aria-label={`来源 ${label} 预览`}
        onEscapeKeyDown={(event) => {
          event.preventDefault(); cancelTimer(); setOpen(false)
          if (document.activeElement !== citationRef.current) {
            returningFocus.current = true
            citationRef.current?.focus({ preventScroll: true })
          }
        }}
        onOpenAutoFocus={(event) => event.preventDefault()} onCloseAutoFocus={(event) => event.preventDefault()}
        onPointerEnter={cancelTimer} onPointerLeave={() => schedule(false, 220)} onFocusCapture={cancelTimer}
        onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) schedule(false, 220) }}>
        <div className="citation-source-meta"><SiteIcon origin={parsed.origin} key={parsed.origin} />
          <span className="citation-source-publisher">{source?.publisher || parsed.hostname}</span>
          {date && <time>{date}</time>}<span className="citation-source-index">来源 {label}</span>
        </div>
        <h3 className="citation-source-title">{source?.title || '来源网页'}</h3>
        <p className="citation-source-summary">{source?.summary || '暂无摘要，可打开原文查看。'}</p>
        <a ref={originalLinkRef} className="citation-source-link" href={url} target="_blank" rel="noopener noreferrer">
          <span>{parsed.hostname}{parsed.pathname === '/' ? '' : parsed.pathname}</span><ExternalLink size={13} aria-label="打开原文" />
        </a>
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>
}

const components = { a: CitationLink }

export function ResearchMarkdown({ sources = EMPTY_SOURCES, ...props }: MessageResponseProps & { sources?: EvidenceSource[] }) {
  return <SourcesContext.Provider value={sources}><MessageResponse {...props} components={components} /></SourcesContext.Provider>
}
