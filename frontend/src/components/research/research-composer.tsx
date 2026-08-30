import { Paperclip, X } from 'lucide-react'
import { useRef, useState } from 'react'

import {
  PromptInput,
  PromptInputBody,
  PromptInputButton,
  PromptInputFooter,
  PromptInputHeader,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools
} from '@/components/ai-elements/prompt-input'
import type { ResearchAttachment } from '@/hooks/use-research-attachments'

const suggestions = [
  '分析宁德时代的盈利拐点',
  '比较高股息策略与债券',
  '复盘今日市场主线'
]

export function ResearchComposer({
  busy,
  attachments,
  error,
  onSend,
  onCancel,
  onFiles,
  onRemoveAttachment
}: {
  busy: boolean
  attachments: ResearchAttachment[]
  error: string
  onSend: (value: string) => void
  onCancel: () => void
  onFiles: (files: File[]) => void
  onRemoveAttachment: (id: string) => void
}) {
  const [value, setValue] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)

  const send = (text: string) => {
    if (!text.trim() || busy) return
    onSend(text.trim())
    setValue('')
  }

  return (
    <div className="composer-wrap">
      {error && <div className="inline-error">{error}</div>}
      <PromptInput
        className="prompt-input"
        accept="image/jpeg,image/png,image/webp"
        maxFiles={4}
        maxFileSize={10 * 1024 * 1024}
        multiple
        onSubmit={({ text }) => send(text || value)}
      >
        {attachments.length > 0 && (
          <PromptInputHeader className="attachment-list">
            {attachments.map((item) => (
              <div className="attachment-chip" key={item.localId}>
                <img src={item.previewUrl} alt="" />
                <span>{item.file.name}<small>{item.status === 'uploading' ? '正在上传…' : item.status === 'ready' ? '已就绪' : '上传失败'}</small></span>
                <button type="button" aria-label={`移除 ${item.file.name}`} onClick={() => onRemoveAttachment(item.localId)}><X size={14} /></button>
              </div>
            ))}
          </PromptInputHeader>
        )}
        <PromptInputBody>
          <PromptInputTextarea
            value={value}
            onChange={(event) => setValue(event.currentTarget.value)}
            placeholder="输入你想研究的公司、行业或投资组合…"
            aria-label="研究问题"
          />
        </PromptInputBody>
        <PromptInputFooter className="prompt-toolbar">
          <PromptInputTools>
            <PromptInputButton type="button" tooltip="添加图片" onClick={() => fileInput.current?.click()}><Paperclip size={17} /></PromptInputButton>
            <input ref={fileInput} hidden type="file" multiple accept="image/jpeg,image/png,image/webp" onChange={(event) => { onFiles(Array.from(event.target.files ?? [])); event.target.value = '' }} />
            <span className="prompt-hint">Enter 发送 · Shift + Enter 换行</span>
          </PromptInputTools>
          <PromptInputSubmit
            status={busy ? 'streaming' : 'ready'}
            onStop={onCancel}
            disabled={!busy && !value.trim()}
          />
        </PromptInputFooter>
      </PromptInput>
      <div className="quick-prompts"><span>试试这样问</span>{suggestions.map((item) => <button type="button" key={item} onClick={() => send(item)}>{item}</button>)}</div>
    </div>
  )
}
