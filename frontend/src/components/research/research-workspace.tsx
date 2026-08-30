import { Menu, X } from 'lucide-react'
import { useState } from 'react'

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

type ResearchWorkspaceProps = {
  authentication: ReturnType<typeof useAuthentication>
  history: ReturnType<typeof useResearchHistory>
  session: ReturnType<typeof useResearchSession>
  attachments: ReturnType<typeof useResearchAttachments>
  evidence: ReturnType<typeof useEvidenceDrawer>
}

export function ResearchWorkspace({ authentication, history, session, attachments, evidence }: ResearchWorkspaceProps) {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [drawerEvidence, setDrawerEvidence] = useState<EvidenceSource[]>([])

  const openCitation = (message: ResearchMessage, index: number) => {
    setDrawerEvidence(message.evidence ?? [])
    evidence.openEvidence(index)
  }

  const sendMessage = (value: string) => {
    void session.sendMessage(value, attachments.attachments.flatMap((item) => item.assetId ? [item.assetId] : []))
    attachments.clearAttachments()
  }

  const sidebarProps = {
    threads: history.threads,
    activeThreadId: history.activeThreadId,
    onActivate: history.activateThread,
    onCreate: history.createThread
  }

  return (
    <main className="workspace">
      <button className="mobile-history" type="button" onClick={() => setSidebarOpen(true)}><Menu size={18} />研究记录</button>
      <div className={sidebarOpen ? 'mobile-sidebar mobile-sidebar--open' : 'mobile-sidebar'}>
        <button className="mobile-sidebar__close" type="button" onClick={() => setSidebarOpen(false)} aria-label="关闭研究记录"><X size={18} /></button>
        <ResearchSidebar {...sidebarProps} onActivate={(id) => { history.activateThread(id); setSidebarOpen(false) }} />
      </div>
      <div className="desktop-sidebar"><ResearchSidebar {...sidebarProps} /></div>
      <section className="research-main">
        <ResearchHeader title={history.activeThread?.title} />
        <ResearchConversation messages={session.messages} onCitation={openCitation} />
        <ResearchComposer
          busy={session.isStreaming}
          attachments={attachments.attachments}
          error={authentication.error || session.error || attachments.error}
          onSend={sendMessage}
          onCancel={session.cancelMessage}
          onFiles={attachments.addFiles}
          onRemoveAttachment={attachments.removeAttachment}
        />
      </section>
      <EvidenceDrawer open={evidence.isOpen} activeIndex={evidence.activeIndex} evidence={drawerEvidence} onOpenChange={(open) => !open && evidence.closeEvidence()} />
    </main>
  )
}
