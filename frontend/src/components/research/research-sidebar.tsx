import { MessageSquareText, MoreHorizontal, PenLine, Search } from 'lucide-react'

import type { ResearchThread } from '../../domain/research'
import { cn } from '@/utils/cn'

export function ResearchSidebar({ threads, activeThreadId, onActivate, onCreate }: {
  threads: ResearchThread[]
  activeThreadId: string
  onActivate: (id: string) => void
  onCreate: () => void
}) {
  return (
    <aside className="research-sidebar">
      <button className="new-research" type="button" onClick={onCreate}><PenLine size={16} /> 新建研究</button>
      <label className="history-search"><Search size={15} /><input placeholder="搜索研究记录" /></label>
      <div className="history-heading"><span>最近研究</span><button type="button" aria-label="更多"><MoreHorizontal size={16} /></button></div>
      <nav className="thread-list" aria-label="研究历史">{threads.map((thread) => (
        <button key={thread.id} type="button" className={cn('thread-item', activeThreadId === thread.id && 'thread-item--active')} onClick={() => onActivate(thread.id)}>
          <MessageSquareText size={16} />
          <span><b>{thread.title}</b><small>{thread.preview}</small></span>
          <time>{thread.updatedAt}</time>
        </button>
      ))}</nav>
    </aside>
  )
}
