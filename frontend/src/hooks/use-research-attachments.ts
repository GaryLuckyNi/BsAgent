import { useCallback, useEffect, useRef, useState } from 'react'

import { uploadImage, type RequestIdentity } from '../services/research-api'

export type ResearchAttachment = {
  localId: string
  file: File
  previewUrl: string
  assetId?: string
  status: 'uploading' | 'ready' | 'failed'
  error?: string
}

const maxAttachments = 4
const maxImageBytes = 10 * 1024 * 1024

export function useResearchAttachments(sessionId: string, identity: RequestIdentity) {
  const [attachments, setAttachments] = useState<ResearchAttachment[]>([])
  const attachmentsRef = useRef<ResearchAttachment[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    attachmentsRef.current = attachments
  }, [attachments])

  useEffect(() => () => {
    attachmentsRef.current.forEach((attachment) => URL.revokeObjectURL(attachment.previewUrl))
  }, [])

  const addFiles = useCallback(async (files: File[]) => {
    setError('')
    if (attachments.length + files.length > maxAttachments) {
      setError(`每次最多添加 ${maxAttachments} 张图片。`)
      return
    }
    const invalid = files.find((file) =>
      !['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > maxImageBytes
    )
    if (invalid) {
      setError(`${invalid.name} 的格式不受支持或超过 10 MB。`)
      return
    }
    const pending = files.map<ResearchAttachment>((file) => ({
      localId: crypto.randomUUID(),
      file,
      previewUrl: URL.createObjectURL(file),
      status: 'uploading'
    }))
    setAttachments((current) => [...current, ...pending])
    await Promise.all(pending.map(async (attachment) => {
      try {
        const asset = await uploadImage(attachment.file, sessionId, identity)
        setAttachments((current) => current.map((item) =>
          item.localId === attachment.localId
            ? { ...item, assetId: asset.asset_id, status: 'ready' }
            : item
        ))
      } catch (caught) {
        setAttachments((current) => current.map((item) =>
          item.localId === attachment.localId
            ? { ...item, status: 'failed', error: errorMessage(caught) }
            : item
        ))
      }
    }))
  }, [attachments.length, identity, sessionId])

  const removeAttachment = useCallback((localId: string) => {
    setAttachments((current) => {
      const target = current.find((item) => item.localId === localId)
      if (target) URL.revokeObjectURL(target.previewUrl)
      return current.filter((item) => item.localId !== localId)
    })
  }, [])

  const clearAttachments = useCallback(() => {
    setAttachments((current) => {
      current.forEach((attachment) => URL.revokeObjectURL(attachment.previewUrl))
      return []
    })
  }, [])
  return { attachments, error, addFiles, removeAttachment, clearAttachments }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '图片上传失败。'
}
