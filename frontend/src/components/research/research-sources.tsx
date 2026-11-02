import { useMemo, useRef, useState } from 'react'
import { ArrowRight, ExternalLink, Files, Search, X } from 'lucide-react'
import { Dialog, Tabs } from 'radix-ui'

import type { EvidenceSource } from '@/domain/research'
import { groupResearchSources, type ResearchSourceGroup } from '@/services/research-sources'
import { SiteIcon } from './site-icon'

export function ResearchSources({ sources, content }: { sources: EvidenceSource[]; content: string }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const panelRef = useRef<HTMLDivElement>(null)
  // 入口只需网页总数；仅打开面板时解析引用，避免逐个正文片段重复解析整篇 Markdown。
  const referenceContent = open ? content : ''
  const groups = useMemo(() => groupResearchSources(sources, referenceContent), [sources, referenceContent])
  const websites = new Set(groups.map((source) => source.hostname)).size
  const citedCount = groups.filter((source) => source.cited).length
  const keyword = query.trim().toLocaleLowerCase()
  const visible = groups.filter((source) => (filter === 'all' || source.cited)
    && `${source.title} ${source.summary || ''} ${source.publisher || ''} ${source.hostname}`.toLocaleLowerCase().includes(keyword))
  if (!groups.length) return null

  return <Dialog.Root open={open} onOpenChange={(value) => {
    if (value) { setQuery(''); setFilter('all') }
    setOpen(value)
  }}>
    <Dialog.Trigger className="research-sources-entry" type="button">
      <span className="research-sources-entry__icon"><Files size={19} aria-hidden="true" /></span>
      <span><strong>研究来源</strong><small>{groups.length} 篇网页 · 来自 {websites} 个网站</small></span>
      <ArrowRight className="research-sources-entry__arrow" size={16} aria-hidden="true" />
    </Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="research-sources-overlay" />
      <Dialog.Content className="research-sources-panel" ref={panelRef} onOpenAutoFocus={(event) => {
        event.preventDefault(); panelRef.current?.focus({ preventScroll: true })
      }}>
        <div className="research-sources-handle" aria-hidden="true" />
        <header className="research-sources-heading">
          <div className="research-sources-eyebrow">本轮资料</div>
          <div className="research-sources-heading__row">
            <Dialog.Title>研究来源</Dialog.Title>
            <Dialog.Close className="research-sources-close" aria-label="关闭研究来源"><X size={19} /></Dialog.Close>
          </div>
          <Dialog.Description>本次搜索找到 {groups.length} 篇网页，正文引用了其中 {citedCount} 篇。</Dialog.Description>
        </header>
        <Tabs.Root className="research-sources-tabs" value={filter} onValueChange={setFilter}>
          <div className="research-sources-filters">
            <label className="research-sources-search"><Search size={15} aria-hidden="true" />
              <input type="search" aria-label="搜索本次来源" placeholder="搜索标题、摘要或网站" autoComplete="off" maxLength={200}
                value={query} onChange={(event) => setQuery(event.target.value)} />
            </label>
            <Tabs.List aria-label="来源范围" className="research-sources-tablist">
              <Tabs.Trigger value="all">全部网页<span>{groups.length}</span></Tabs.Trigger>
              <Tabs.Trigger value="cited">正文引用<span>{citedCount}</span></Tabs.Trigger>
            </Tabs.List>
          </div>
          <Tabs.Content key={filter} value={filter} className="research-sources-list">
            <span className="sr-only" role="status">显示 {visible.length} 篇网页</span>
            {visible.map((source) => <SourceItem key={source.url} source={source} />)}
            {!visible.length && <div className="research-sources-empty" role="status">
              <Search size={24} aria-hidden="true" /><strong>{keyword ? '没有找到匹配的网页' : '正文暂未引用网页'}</strong>
              <p>{keyword ? '换个关键词，或清空搜索查看全部结果' : '可以切换到全部网页查看本次搜索结果'}</p>
            </div>}
          </Tabs.Content>
        </Tabs.Root>
        <footer className="research-sources-footer"><ExternalLink size={13} aria-hidden="true" />展示搜索摘要，点击标题可在新标签页查看原文</footer>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}

function SourceItem({ source }: { source: ResearchSourceGroup }) {
  const date = source.publishedAt?.replace(/^(\d{4})-(\d{2})-(\d{2}).*$/, '$1/$2/$3')
  return <article className="research-source-item">
    <div className="research-source-item__meta">
      <SiteIcon origin={new URL(source.url).origin} />
      <span className="research-source-item__publisher">{source.publisher || source.hostname}</span>
      {date && <time dateTime={source.publishedAt}>{date}</time>}
      <span className="research-source-item__number" aria-label={`来源 ${source.indices.join('、')}`}>{source.indices.join('、')}</span>
    </div>
    <h3><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title || source.hostname}<ExternalLink size={12} aria-hidden="true" /></a></h3>
    <p className="research-source-item__summary">{source.summary || '暂无摘要'}</p>
    <div className="research-source-item__footer"><span>{source.hostname}</span>{source.cited && <span className="research-source-item__cited">正文已引用</span>}</div>
  </article>
}
