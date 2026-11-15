import { Menu, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { StickToBottomContext } from 'use-stick-to-bottom'

import type { EvidenceSource, ResearchMessage } from '@/domain/research'
import type { useAuthentication } from '@/hooks/use-authentication'
import type { useEvidenceDrawer } from '@/hooks/use-evidence-drawer'
import type { useResearchAttachments } from '@/hooks/use-research-attachments'
import type { useResearchHistory } from '@/hooks/use-research-history'
import type { useResearchSession } from '@/hooks/use-research-session'

import { EvidenceDrawer } from './evidence-drawer'
import { ResearchComposer } from './research-composer'
import { ResearchConversation } from './research-conversation'
import { ResearchHeader } from './research-header'
import { ResearchSidebar } from './research-sidebar'
import { ResearchSearchDialog } from './research-search-dialog'

type ResearchWorkspaceProps = {
  authentication: ReturnType<typeof useAuthentication>
  history: ReturnType<typeof useResearchHistory>
  session: ReturnType<typeof useResearchSession>
  attachments: ReturnType<typeof useResearchAttachments>
  evidence: ReturnType<typeof useEvidenceDrawer>
}

export function ResearchWorkspace({ authentication, history, session, attachments, evidence }: ResearchWorkspaceProps) {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return window.localStorage.getItem('investwise.sidebar-collapsed') === 'true' } catch { return false }
  })
  const conversationContextRef = useRef<StickToBottomContext>(null)
  const layoutFrameRef = useRef<number | undefined>(undefined)
  useEffect(() => () => { if (layoutFrameRef.current !== undefined) cancelAnimationFrame(layoutFrameRef.current) }, [])

  const toggleSidebar = () => {
    if (layoutFrameRef.current !== undefined) cancelAnimationFrame(layoutFrameRef.current)
    const context = conversationContextRef.current
    const scroll = context?.scrollRef.current
    if (context && scroll && !context.isAtBottom) {
      const top = scroll.getBoundingClientRect().top
      const anchor = [...scroll.querySelectorAll<HTMLElement>('[data-message-id]')].find((item) => item.getBoundingClientRect().bottom > top)
      if (anchor) {
        const offset = anchor.getBoundingClientRect().top - top
        context.stopScroll()
        const started = performance.now()
        const duration = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 0 : 220
        const preserve = (now: number) => {
          if (!anchor.isConnected || !scroll.isConnected) return
          scroll.scrollTop += anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top - offset
          if (now - started < duration) layoutFrameRef.current = requestAnimationFrame(preserve)
        }
        layoutFrameRef.current = requestAnimationFrame(preserve)
      }
    }
    const next = !sidebarCollapsed
    setSidebarCollapsed(next)
    try { window.localStorage.setItem('investwise.sidebar-collapsed', String(next)) } catch { /* 存储不可用时仍可切换。 */ }
  }
  const [drawerEvidence, setDrawerEvidence] = useState<EvidenceSource[]>([])
  const [searchOpen, setSearchOpen] = useState(false)
  const [locatedTarget, setLocatedTarget] = useState<{ messageId: string; requestId: string }>()
  const [navigationStatus, setNavigationStatus] = useState('')
  const { navigationTarget } = history
  const { revealMessage, isInitializing } = session

  useEffect(() => {
    setLocatedTarget(undefined)
    if (!navigationTarget) { setNavigationStatus(''); return }
    // 子组件 effect 先于父级会话初始化 effect；等真实恢复结束再补历史。
    if (isInitializing) { setNavigationStatus('正在定位历史消息…'); return }
    const abort = new AbortController()
    setNavigationStatus('正在定位历史消息…')
    void revealMessage(navigationTarget.messageId, navigationTarget.sequenceNo, abort.signal).then(() => {
      if (abort.signal.aborted) return
      setLocatedTarget(navigationTarget)
      setNavigationStatus('')
    }).catch((caught) => {
      if (!abort.signal.aborted) setNavigationStatus(caught instanceof Error ? caught.message : '定位失败，请重新搜索。')
    })
    return () => abort.abort()
  }, [navigationTarget, revealMessage, isInitializing])

  const openCitation = (message: ResearchMessage, index: number) => {
    setDrawerEvidence(message.evidence ?? [])
    evidence.openEvidence(index)
  }

  const sendMessage = (value: string) => {
    if (history.navigationTarget) history.activateThread(history.activeThreadId)
    void session.sendMessage(value, attachments.attachments.flatMap((item) => item.assetId ? [item.assetId] : []))
    attachments.clearAttachments()
  }

  const sidebarProps = {
    threads: history.threads,
    activeThreadId: history.activeThreadId,
    onActivate: history.activateThread,
    onCreate: history.createThread,
    onSearch: () => { setSidebarOpen(false); setSearchOpen(true) },
    onDelete: history.deleteThread
  }

  return (
    <main className={sidebarCollapsed ? 'workspace workspace--sidebar-collapsed' : 'workspace'}>
      <button className="mobile-history" type="button" onClick={() => setSidebarOpen(true)}><Menu size={18} />研究记录</button>
      <div className={sidebarOpen ? 'mobile-sidebar mobile-sidebar--open' : 'mobile-sidebar'}>
        <button className="mobile-sidebar__close" type="button" onClick={() => setSidebarOpen(false)} aria-label="关闭研究记录"><X size={18} /></button>
        <ResearchSidebar {...sidebarProps} onActivate={(id) => { history.activateThread(id); setSidebarOpen(false) }} />
      </div>
      <div className="desktop-sidebar"><ResearchSidebar {...sidebarProps} collapsed={sidebarCollapsed} onToggleCollapsed={toggleSidebar} /></div>
      <section className="research-main">
        <ResearchHeader title={history.activeThread?.title} preview={history.activeThread?.preview} />
        <ResearchConversation
          messages={session.messages}
          contextRef={conversationContextRef}
          submittedMessageId={session.submittedMessageId}
          navigationTarget={locatedTarget}
          navigationStatus={navigationStatus}
          onCitation={openCitation}
          onUserInputSelect={(messageId, value) => {
            void session.resumeUserInput(messageId, value)
          }}
        />
        <ResearchComposer
          busy={session.isStreaming}
          attachments={attachments.attachments}
          error={authentication.error || session.error || attachments.error || history.error}
          onSend={sendMessage}
          onCancel={session.cancelMessage}
          onFiles={attachments.addFiles}
          onRemoveAttachment={attachments.removeAttachment}
        />
      </section>
      <ResearchSearchDialog open={searchOpen} onOpenChange={setSearchOpen} identity={authentication.identity} onSelect={history.navigateToMessage} />
      <EvidenceDrawer open={evidence.isOpen} activeIndex={evidence.activeIndex} evidence={drawerEvidence} onOpenChange={(open) => !open && evidence.closeEvidence()} />
    </main>
  )
}
