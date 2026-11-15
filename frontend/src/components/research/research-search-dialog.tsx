import { LoaderCircle, MessageSquareText, Search, X } from 'lucide-react'
import { Dialog } from 'radix-ui'
import { useEffect, useId, useRef, useState } from 'react'

import type { RequestIdentity } from '@/services/research-api'
import { searchResearchMessages, type ResearchSearchResult } from '@/services/research-search'

export function ResearchSearchDialog({ open, onOpenChange, identity, onSelect }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  identity: RequestIdentity
  onSelect: (result: ResearchSearchResult) => void
}) {
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<ResearchSearchResult[]>([])
  const [nextOffset, setNextOffset] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)
  const selectedRef = useRef(false)
  const resultRef = useRef<HTMLDivElement>(null)
  const controller = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const listId = useId()
  const keyword = query.trim()

  const load = async (value: string, offset: number, signal: AbortSignal, epoch: number) => {
    try {
      const page = await searchResearchMessages(identity, value, offset, signal)
      if (signal.aborted || epoch !== generation.current) return
      setItems((current) => offset ? [...new Map([...current, ...page.items].map((item) => [item.message_id, item])).values()] : page.items)
      setNextOffset(page.next_offset)
      setError('')
    } catch (caught) {
      if (!signal.aborted && epoch === generation.current) setError(caught instanceof Error ? caught.message : '搜索失败，请重试。')
    } finally {
      if (!signal.aborted && epoch === generation.current) setLoading(false)
    }
  }

  useEffect(() => {
    const epoch = ++generation.current
    const abort = new AbortController()
    controller.current = abort
    setItems([])
    setNextOffset(null)
    setError('')
    setActiveIndex(0)
    setLoading(open && !!keyword)
    if (!open || !keyword) return () => abort.abort()
    const timer = window.setTimeout(() => { void load(keyword, 0, abort.signal, epoch) }, 250)
    return () => { clearTimeout(timer); abort.abort() }
  }, [open, keyword, retry, identity.tenantId, identity.userId, identity.accessToken])

  useEffect(() => {
    const option = resultRef.current?.querySelector<HTMLElement>(`[data-result-index="${activeIndex}"]`)
    option?.scrollIntoView?.({ block: 'nearest' })
  }, [activeIndex])

  const select = (result: ResearchSearchResult) => { selectedRef.current = true; onSelect(result); onOpenChange(false) }
  const loadMore = () => {
    if (loading || nextOffset === null || !controller.current) return
    setLoading(true)
    void load(keyword, nextOffset, controller.current.signal, generation.current)
  }

  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal>
      <Dialog.Overlay className="research-search-overlay" />
      <Dialog.Content className="research-search-dialog" onOpenAutoFocus={(event) => {
        event.preventDefault()
        selectedRef.current = false
        returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
        inputRef.current?.focus()
      }} onCloseAutoFocus={(event) => {
        event.preventDefault()
        if (!selectedRef.current && returnFocusRef.current?.isConnected) returnFocusRef.current.focus()
      }}>
        <Dialog.Title className="sr-only">搜索研究记录</Dialog.Title>
        <Dialog.Description className="sr-only">搜索全部历史提问与回答，选择结果后跳转到对应消息。</Dialog.Description>
        <div className="research-search-dialog__input">
          <Search size={21} aria-hidden="true" />
          <input ref={inputRef} role="combobox" aria-label="搜索全部聊天内容" aria-expanded={items.length > 0}
            aria-autocomplete="list" aria-controls={listId} aria-activedescendant={items[activeIndex] ? `${listId}-${activeIndex}` : undefined}
            placeholder="搜索全部聊天内容…" maxLength={200} value={query}
            onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return
              if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault()
                setActiveIndex((index) => Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1))))
              }
              if (event.key === 'Enter' && items[activeIndex]) { event.preventDefault(); select(items[activeIndex]) }
            }} />
          <Dialog.Close className="research-search-dialog__close" aria-label="关闭搜索"><X size={21} /></Dialog.Close>
        </div>
        <div className="research-search-dialog__results" ref={resultRef}>
          {!keyword && <div className="research-search-empty"><Search size={26} /><strong>搜索全部研究记录</strong><p>输入关键词，查找历史提问与回答</p></div>}
          {keyword && loading && items.length === 0 && <div className="research-search-empty" role="status"><LoaderCircle className="session-delete-spinner" size={22} />正在搜索…</div>}
          {keyword && !loading && !error && items.length === 0 && <div className="research-search-empty" role="status"><Search size={26} /><strong>没有找到相关内容</strong><p>试试公司名称、关键词或问题中的一部分</p></div>}
          <div id={listId} role="listbox" aria-label="搜索结果">
            {items.map((item, index) => <button type="button" role="option" aria-selected={index === activeIndex}
              id={`${listId}-${index}`} data-result-index={index} key={item.message_id} className="research-search-result"
              onMouseEnter={() => setActiveIndex(index)} onFocus={() => setActiveIndex(index)} onClick={() => select(item)}>
              <span className="research-search-result__icon"><MessageSquareText size={20} /></span>
              <span className="research-search-result__body">
                <span className="research-search-result__heading"><strong>{plainTitle(item.title) || '新建投资研究'}</strong><time dateTime={item.created_at}>{formatSearchTime(item.created_at)}</time></span>
                <span className="research-search-result__snippet"><span className="research-search-result__role">{item.role === 'user' ? '提问' : '回答'}</span><HighlightedText text={item.snippet} keyword={keyword} /></span>
              </span>
            </button>)}
          </div>
          {error && <div className="research-search-error" role="alert"><span>{error}</span><button type="button" onClick={() => items.length ? loadMore() : setRetry((value) => value + 1)}>重试</button></div>}
          {nextOffset !== null && !error && <button className="research-search-more" type="button" disabled={loading} onClick={loadMore}>{loading ? '正在加载…' : '加载更多'}</button>}
        </div>
        <div className="research-search-dialog__footer"><span>{items.length ? `已显示 ${items.length} 条匹配消息` : '搜索历史聊天内容'}</span><span>↑ ↓ 选择 · Enter 跳转 · Esc 关闭</span></div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}

function HighlightedText({ text, keyword }: { text: string; keyword: string }) {
  if (!keyword) return text
  const parts = []
  const lower = text.toLocaleLowerCase()
  const needle = keyword.toLocaleLowerCase()
  let start = 0
  let index = lower.indexOf(needle)
  while (index !== -1) {
    parts.push(text.slice(start, index), <mark key={index}>{text.slice(index, index + keyword.length)}</mark>)
    start = index + keyword.length
    index = lower.indexOf(needle, start)
  }
  parts.push(text.slice(start))
  return <>{parts}</>
}

function plainTitle(value: string) { return value.replace(/[#*`>|]/g, '').replace(/\s+/g, ' ').trim() }
function formatSearchTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return date.toLocaleDateString('zh-CN', { ...(date.getFullYear() !== today.getFullYear() ? { year: 'numeric' as const } : {}), month: 'numeric', day: 'numeric' })
}
