import { useOutletContext } from 'react-router-dom'

import { ResearchWorkspace } from '@/components/research/research-workspace'
import { useEvidenceDrawer } from '@/hooks/use-evidence-drawer'
import { useResearchAttachments } from '@/hooks/use-research-attachments'
import { useResearchHistory } from '@/hooks/use-research-history'
import { useResearchSession } from '@/hooks/use-research-session'
import type { AppOutletContext } from '@/layouts/app-layout'

export function AiResearchView() {
  const { authentication } = useOutletContext<AppOutletContext>()
  const history = useResearchHistory()
  const session = useResearchSession(authentication.identity)
  const attachments = useResearchAttachments(session.sessionId, authentication.identity)
  const evidence = useEvidenceDrawer()

  return <ResearchWorkspace authentication={authentication} history={history} session={session} attachments={attachments} evidence={evidence} />
}
