import { Sparkles } from 'lucide-react'

export function ResearchHeader({ title, preview }: { title?: string; preview?: string }) {
  return (
    <header className="research-header">
      <div>
        <span className="research-kicker"><Sparkles size={14} /> AI 研究</span>
        <h1 title={title}>{title || '新建投资研究'}</h1>
        <p title={preview}>{preview || '输入问题，开始研究'}</p>
      </div>
    </header>
  )
}
