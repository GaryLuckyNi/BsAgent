import {
  BarChart3, Bell, Bot, BriefcaseBusiness, ChevronDown, CircleUserRound,
  Compass, Copy, FileText, LineChart, Menu, Search, Sparkles, Star, ThumbsDown, ThumbsUp, X
} from 'lucide-react'
import { useState } from 'react'

import { Conversation, ConversationContent } from './components/ai-elements/conversation'
import { Message, MessageActions, MessageContent } from './components/ai-elements/message'
import { PromptInput } from './components/ai-elements/prompt-input'
import { Reasoning } from './components/ai-elements/reasoning'
import { EvidenceDrawer } from './components/research/evidence-drawer'
import { ResearchSidebar } from './components/research/research-sidebar'
import type { EvidenceSource, ResearchMessage } from './domain/research'
import { useEvidenceDrawer } from './hooks/use-evidence-drawer'
import { useAuthentication } from './hooks/use-authentication'
import { useResearchAttachments } from './hooks/use-research-attachments'
import { useResearchHistory } from './hooks/use-research-history'
import { useResearchSession } from './hooks/use-research-session'

const navItems = [
  { label: '市场', icon: LineChart }, { label: '自选', icon: Star }, { label: '资讯', icon: FileText },
  { label: '持仓', icon: BriefcaseBusiness }, { label: '策略与提醒', icon: Bell }, { label: 'AI 研究', icon: Bot, active: true }
]

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const authentication = useAuthentication()
  const history = useResearchHistory()
  const session = useResearchSession(authentication.identity)
  const files = useResearchAttachments(session.sessionId, authentication.identity)
  const drawer = useEvidenceDrawer()
  const [drawerEvidence, setDrawerEvidence] = useState<EvidenceSource[]>([])

  const openCitation = (message: ResearchMessage, index: number) => {
    setDrawerEvidence(message.evidence ?? [])
    drawer.openEvidence(index)
  }
  const submit = (value: string) => {
    void session.sendMessage(value, files.attachments.flatMap((item) => item.assetId ? [item.assetId] : []))
    files.clearAttachments()
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark"><BarChart3 size={20} /></span><b>InvestWise</b><i>智能投资研究平台</i></div>
        <nav className="main-nav" aria-label="主导航">{navItems.map(({ label, icon: Icon, active }) => (
          <button key={label} type="button" className={active ? 'active' : ''} disabled={!active} title={!active ? '功能规划中' : undefined}><Icon size={16} />{label}</button>
        ))}</nav>
        <div className="top-actions"><button type="button" aria-label="全局搜索"><Search size={18} /></button><button type="button" aria-label="通知"><Bell size={18} /></button><button className="profile" type="button" onClick={() => void authentication.toggleAuthentication()} title={authentication.enabled ? '点击登录或退出' : undefined}><CircleUserRound size={19} /><span>{authentication.status === 'anonymous' ? '登录' : authentication.displayName}</span><ChevronDown size={13} /></button></div>
      </header>

      <main className="workspace">
        <button className="mobile-history" type="button" onClick={() => setSidebarOpen(true)}><Menu size={18} />研究记录</button>
        <div className={sidebarOpen ? 'mobile-sidebar mobile-sidebar--open' : 'mobile-sidebar'}>
          <button className="mobile-sidebar__close" type="button" onClick={() => setSidebarOpen(false)}><X size={18} /></button>
          <ResearchSidebar threads={history.threads} activeThreadId={history.activeThreadId} onActivate={(id) => { history.activateThread(id); setSidebarOpen(false) }} onCreate={history.createThread} />
        </div>
        <div className="desktop-sidebar"><ResearchSidebar threads={history.threads} activeThreadId={history.activeThreadId} onActivate={history.activateThread} onCreate={history.createThread} /></div>

        <section className="research-main">
          <header className="research-header">
            <div><span className="research-kicker"><Sparkles size={14} /> AI 研究</span><h1>{history.activeThread?.title ?? '投资研究'}</h1><p>整合市场、公告与宏观信息，形成可追溯的投资观点。</p></div>
            <div className="status-chip"><span className={session.isOnline ? 'online' : ''} />{session.isOnline === null ? '正在连接' : session.isOnline ? '研究服务在线' : '演示模式'}</div>
          </header>

          <Conversation>
            <ConversationContent>{session.messages.map((message) => (
              <Message key={message.id} from={message.role}>
                {message.role === 'assistant' && <div className="assistant-avatar"><Compass size={17} /></div>}
                <div className="message-body">
                  {message.role === 'assistant' && <div className="message-author">InvestWise 研究助手 <span>{message.status === 'streaming' ? '正在研究' : '研究完成'}</span></div>}
                  <Reasoning content={message.thinking ?? ''} streaming={message.status === 'streaming'} />
                  <MessageContent>{message.content || '正在整理研究结果…'}</MessageContent>
                  {message.citations && message.citations.length > 0 && <div className="citation-row">
                    <span>引用证据</span>{message.citations.map((index) => <button key={index} type="button" onClick={() => openCitation(message, index)} aria-label={`打开第 ${index} 条证据`}>{index}</button>)}
                  </div>}
                  {message.role === 'assistant' && message.status !== 'streaming' && <MessageActions><button type="button" aria-label="复制回答" onClick={() => void navigator.clipboard.writeText(message.content)}><Copy size={14} /></button><button type="button" aria-label="有帮助"><ThumbsUp size={14} /></button><button type="button" aria-label="无帮助"><ThumbsDown size={14} /></button></MessageActions>}
                </div>
              </Message>
            ))}</ConversationContent>
          </Conversation>

          <div className="composer-wrap">
            {(authentication.error || session.error || files.error) && <div className="inline-error">{authentication.error || session.error || files.error}</div>}
            <PromptInput busy={session.isStreaming} attachments={files.attachments} onSubmit={submit} onCancel={session.cancelMessage} onFiles={files.addFiles} onRemoveAttachment={files.removeAttachment} />
            <div className="quick-prompts"><span>试试这样问</span>{['分析宁德时代的盈利拐点', '比较高股息策略与债券', '复盘今日市场主线'].map((item) => <button type="button" key={item} onClick={() => submit(item)}>{item}</button>)}</div>
          </div>
        </section>
      </main>
      <EvidenceDrawer open={drawer.isOpen} activeIndex={drawer.activeIndex} evidence={drawerEvidence} onOpenChange={(open) => !open && drawer.closeEvidence()} />
    </div>
  )
}
