import { LoaderCircle, MessageSquareText, MoreHorizontal, PanelLeftClose, PanelLeftOpen, PenLine, Search, Trash2, X } from 'lucide-react'
import { Dialog as DialogPrimitive } from 'radix-ui'
import { useEffect, useId, useRef, useState } from 'react'

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

import type { ResearchThread } from '../../domain/research'
import { cn } from '@/utils/cn'

export function ResearchSidebar({ threads, activeThreadId, onActivate, onCreate, onSearch, onDelete, collapsed = false, onToggleCollapsed }: {
  threads: ResearchThread[]
  activeThreadId: string
  onActivate: (id: string) => void
  onCreate: () => void
  onSearch: () => void
  onDelete: (id: string) => Promise<void>
  collapsed?: boolean
  onToggleCollapsed?: () => void
}) {
  const [deleteTarget, setDeleteTarget] = useState<ResearchThread | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const cancelRef = useRef<HTMLButtonElement>(null)
  const createRef = useRef<HTMLButtonElement>(null)
  const actionRefs = useRef(new Map<string, HTMLButtonElement>())
  const returnFocusRef = useRef<HTMLButtonElement | null>(null)
  const [isScrolling, setIsScrolling] = useState(false)
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const expandRef = useRef<HTMLButtonElement>(null)
  const collapseRef = useRef<HTMLButtonElement>(null)
  const wasCollapsedRef = useRef(collapsed)
  const contentId = useId()

  useEffect(() => {
    if (wasCollapsedRef.current === collapsed) return
    wasCollapsedRef.current = collapsed
    const toggleButton = collapsed ? expandRef.current : collapseRef.current
    toggleButton?.focus({ preventScroll: true })
  }, [collapsed])

  useEffect(() => () => clearTimeout(scrollTimerRef.current), [])

  const showScrollbar = () => {
    setIsScrolling(true)
    clearTimeout(scrollTimerRef.current)
    scrollTimerRef.current = setTimeout(() => setIsScrolling(false), 800)
  }

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return
    setDeleting(true)
    setDeleteError('')
    try {
      await onDelete(deleteTarget.id)
      setDeleteTarget(null)
    } catch (caught) {
      setDeleteError(caught instanceof Error ? caught.message : '删除失败，请重试。')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <aside className={cn('research-sidebar', collapsed && 'research-sidebar--collapsed')}>
      {collapsed && <div className="sidebar-rail">
        <button ref={expandRef} className="sidebar-icon-button" type="button" aria-label="展开侧栏" title="展开侧栏" aria-expanded={false} aria-controls={contentId} onClick={onToggleCollapsed}><PanelLeftOpen size={19} /></button>
        <span className="sidebar-rail__divider" />
        <button className="sidebar-icon-button sidebar-rail__create" type="button" aria-label="新建研究" title="新建研究" onClick={onCreate}><PenLine size={18} /></button>
        <button className="sidebar-icon-button sidebar-search-button" type="button" aria-label="搜索研究记录" title="搜索研究记录" onClick={onSearch}><Search size={19} /></button>
      </div>}
      <div id={contentId} className="sidebar-expanded-content" inert={collapsed} aria-hidden={collapsed || undefined}>
      <div className="sidebar-toolbar"><span>研究空间</span><div className="sidebar-toolbar__actions"><button className="sidebar-search-button" type="button" aria-label="搜索研究记录" title="搜索研究记录" onClick={onSearch}><Search size={19} /></button>{onToggleCollapsed && <button ref={collapseRef} className="sidebar-icon-button" type="button" aria-label="收起侧栏" title="收起侧栏" aria-expanded={true} aria-controls={contentId} onClick={onToggleCollapsed}><PanelLeftClose size={19} /></button>}</div></div>
      <button ref={createRef} className="new-research" type="button" onClick={onCreate}><PenLine size={16} /> 新建研究</button>
      <div className="history-heading"><span>最近研究</span></div>
      <nav className={cn('thread-list', isScrolling && 'thread-list--scrolling')} onScroll={showScrollbar} aria-label="研究历史">{threads.map((thread) => (
        <div key={thread.id} className={cn('thread-item', activeThreadId === thread.id && 'thread-item--active')}>
          <button className="thread-item__select" type="button" aria-label={thread.title} aria-current={activeThreadId === thread.id ? 'true' : undefined} onClick={() => onActivate(thread.id)}>
            <MessageSquareText size={16} />
            <span><b>{thread.title}</b><small>{thread.preview}</small></span>
            <time dateTime={thread.updatedAt}>{formatUpdatedAt(thread.updatedAt)}</time>
          </button>
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <button ref={(element) => { if (element) actionRefs.current.set(thread.id, element); else actionRefs.current.delete(thread.id) }} className="thread-item__action" type="button" aria-label="更多操作" title="更多操作"><MoreHorizontal size={16} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" sideOffset={6} className="session-action-menu" onCloseAutoFocus={(event) => { if (deleteTarget) event.preventDefault() }}>
              <DropdownMenuItem className="session-action-menu__delete" onSelect={() => { returnFocusRef.current = actionRefs.current.get(thread.id) || null; setDeleteError(''); setDeleteTarget(thread) }}><Trash2 size={16} />删除</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ))}</nav>
      </div>
      <DialogPrimitive.Root open={deleteTarget !== null} onOpenChange={(open) => { if (!open && !deleting) setDeleteTarget(null) }}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="session-delete-overlay" />
          <DialogPrimitive.Content className="session-delete-dialog"
            onOpenAutoFocus={(event) => { event.preventDefault(); cancelRef.current?.focus() }}
            onCloseAutoFocus={(event) => { event.preventDefault(); (returnFocusRef.current?.isConnected ? returnFocusRef.current : createRef.current)?.focus() }}
            onEscapeKeyDown={(event) => { if (deleting) event.preventDefault() }}
            onPointerDownOutside={(event) => { if (deleting) event.preventDefault() }}>
            <div className="session-delete-dialog__body">
              <div className="session-delete-dialog__heading">
                <span className="session-delete-dialog__icon"><Trash2 size={19} aria-hidden="true" /></span>
                <DialogPrimitive.Title>删除这条研究？</DialogPrimitive.Title>
                <DialogPrimitive.Close className="session-delete-dialog__close" disabled={deleting} aria-label="关闭删除确认框"><X size={17} /></DialogPrimitive.Close>
              </div>
              <DialogPrimitive.Description className="session-delete-dialog__description"><strong>「{deleteTarget?.title}」</strong>及其聊天记录将从研究列表中移除。</DialogPrimitive.Description>
              <p className="session-delete-dialog__warning">此操作无法在页面中撤销。</p>
              {deleteError && <p className="session-delete-error" role="alert">{deleteError}</p>}
            </div>
            <div className="session-delete-dialog__footer">
              <button ref={cancelRef} className="session-delete-dialog__cancel" type="button" disabled={deleting} onClick={() => setDeleteTarget(null)}>取消</button>
              <button className="session-delete-dialog__confirm" type="button" disabled={deleting} onClick={() => { void confirmDelete() }}>{deleting && <LoaderCircle size={14} className="session-delete-spinner" aria-hidden="true" />}{deleting ? '正在删除…' : '删除研究'}</button>
            </div>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </aside>
  )
}

function formatUpdatedAt(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  if (now.getTime() - date.getTime() < 60_000) return '刚刚'
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) return '昨天'
  return date.toLocaleDateString('zh-CN', {
    ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' as const } : {}),
    month: 'numeric', day: 'numeric'
  })
}
