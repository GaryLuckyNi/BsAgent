import { useOutletContext } from 'react-router-dom'
import { useEffect } from 'react'

import { ResearchWorkspace } from '@/components/research/research-workspace'
import { useEvidenceDrawer } from '@/hooks/use-evidence-drawer'
import { useResearchAttachments } from '@/hooks/use-research-attachments'
import { useResearchHistory } from '@/hooks/use-research-history'
import { useResearchSession } from '@/hooks/use-research-session'
import type { AppOutletContext } from '@/layouts/app-layout'

export function AiResearchView() {
  const { authentication } = useOutletContext<AppOutletContext>()
  return <ResearchHistoryView key={JSON.stringify(authentication.identity)} authentication={authentication} />
}

function ResearchHistoryView({ authentication }: Pick<AppOutletContext, 'authentication'>) {
  const history = useResearchHistory(authentication.identity)
  return <ResearchSessionView key={history.activeThreadId} authentication={authentication} history={history} />
}

function ResearchSessionView({ authentication, history }: Pick<AppOutletContext, 'authentication'> & {
  history: ReturnType<typeof useResearchHistory>
}) {
  const session = useResearchSession(authentication.identity, history.activeThreadId)
  const attachments = useResearchAttachments(session.sessionId, authentication.identity)
  const evidence = useEvidenceDrawer()
  const { updateConversation, refresh } = history
  useEffect(() => {
    if (session.submittedMessageId) updateConversation(session.sessionId, session.messages)
  }, [updateConversation, session.sessionId, session.messages, session.submittedMessageId])
  const latestMessage = session.messages.at(-1)
  useEffect(() => {
    if (latestMessage?.role === 'assistant'
      && latestMessage.status !== 'streaming' && latestMessage.status !== 'stopping') void refresh()
  }, [refresh, latestMessage?.id, latestMessage?.role, latestMessage?.status])

  return <ResearchWorkspace authentication={authentication} history={history} session={session} attachments={attachments} evidence={evidence} />
}
