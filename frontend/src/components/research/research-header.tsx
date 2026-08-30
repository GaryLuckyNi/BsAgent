import { Sparkles } from 'lucide-react'

export function ResearchHeader({ title }: { title?: string }) {
  return (
    <header className="research-header">
      <div>
        <span className="research-kicker"><Sparkles size={14} /> AI 研究</span>
        <h1>{title ?? '投资研究'}</h1>
        <p>整合市场、公告与宏观信息，形成可追溯的投资观点。</p>
      </div>
    </header>
  )
}
