import { ChevronDown, Sparkles } from 'lucide-react'
import { useState } from 'react'

export function Reasoning({ content, streaming }: { content: string; streaming?: boolean }) {
  const [open, setOpen] = useState(false)
  if (!content) return null
  return (
    <div className="reasoning">
      <button type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <Sparkles size={14} />
        <span>{streaming ? '正在整理研究思路…' : '查看研究过程'}</span>
        <ChevronDown className={open ? 'rotate' : ''} size={14} />
      </button>
      {open && <p>{content}</p>}
    </div>
  )
}
