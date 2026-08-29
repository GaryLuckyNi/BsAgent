import { ArrowUp, Paperclip, Square, X } from 'lucide-react'
import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react'

import type { ResearchAttachment } from '../../hooks/use-research-attachments'

export function PromptInput({
  busy,
  attachments,
  onSubmit,
  onCancel,
  onFiles,
  onRemoveAttachment
}: {
  busy: boolean
  attachments: ResearchAttachment[]
  onSubmit: (value: string) => void
  onCancel: () => void
  onFiles: (files: File[]) => void
  onRemoveAttachment: (id: string) => void
}) {
  const [value, setValue] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const submit = (event?: FormEvent) => {
    event?.preventDefault()
    if (!value.trim() || busy) return
    onSubmit(value)
    setValue('')
  }
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }
  return (
    <form className="prompt-input" onSubmit={submit}>
      {attachments.length > 0 && <div className="attachment-list">{attachments.map((item) => (
        <div className="attachment-chip" key={item.localId}>
          <img src={item.previewUrl} alt="" />
          <span>{item.file.name}<small>{item.status === 'uploading' ? '正在上传…' : item.status === 'ready' ? '已就绪' : '上传失败'}</small></span>
          <button type="button" aria-label={`移除 ${item.file.name}`} onClick={() => onRemoveAttachment(item.localId)}><X size={14} /></button>
        </div>
      ))}</div>}
      <textarea
        rows={3}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder="输入你想研究的公司、行业或投资组合…"
        aria-label="研究问题"
      />
      <div className="prompt-toolbar">
        <div>
          <button className="icon-button" type="button" onClick={() => fileInput.current?.click()} aria-label="添加图片"><Paperclip size={17} /></button>
          <input ref={fileInput} hidden type="file" multiple accept="image/jpeg,image/png,image/webp" onChange={(event) => onFiles(Array.from(event.target.files ?? []))} />
          <span className="prompt-hint">Enter 发送 · Shift + Enter 换行</span>
        </div>
        {busy
          ? <button className="send-button send-button--stop" type="button" onClick={onCancel} aria-label="停止生成"><Square size={14} fill="currentColor" /></button>
          : <button className="send-button" type="submit" disabled={!value.trim()} aria-label="发送"><ArrowUp size={18} /></button>}
      </div>
    </form>
  )
}
