import { useCallback, useEffect, useRef, useState } from 'react'

import { resolveConversationSession, saveConversationSession } from '../conversation-session'
import type { ResearchMessage, ResearchThread } from '../domain/research'
import { deleteResearchSession, loadResearchSessions, type RequestIdentity } from '../services/research-api'

export type MessageNavigationTarget = { sessionId: string; messageId: string; sequenceNo: number; requestId: string }
type MessageSearchMatch = { session_id: string; message_id: string; sequence_no: number; title: string; snippet: string; created_at: string }

export function useResearchHistory(identity: RequestIdentity) {
  const [threads, setThreads] = useState<ResearchThread[]>([])
  const [activeThreadId, setActiveThreadId] = useState(() => resolveConversationSession(
    window.localStorage, identity, newSessionId
  ))
  const [error, setError] = useState('')
  const [navigationTarget, setNavigationTarget] = useState<MessageNavigationTarget>()
  const searchedThreads = useRef(new Map<string, ResearchThread>())
  const activeId = useRef(activeThreadId)
  const visibleThreads = useRef(threads)
  visibleThreads.current = threads
  const deletedIds = useRef(new Set<string>())
  const localUpdates = useRef(new Map<string, ResearchThread>())
  const remoteThreads = useRef(new Map<string, ResearchThread>())
  const disposed = useRef(false)
  const request = useRef(0)

  const refresh = useCallback(async () => {
    const current = ++request.current
    try {
      const records = await loadResearchSessions(identity)
      if (disposed.current || current !== request.current) return
      setError('')
      const merged = new Map(records.filter((item) => !deletedIds.current.has(item.session_id)).map((item) => [item.session_id, {
        id: item.session_id,
        title: plainText(item.title, 80) || '新建投资研究',
        preview: plainText(item.preview, 160), updatedAt: item.updated_at
      }]))
      for (const [id, thread] of merged) remoteThreads.current.set(id, thread)
      for (const [id, local] of localUpdates.current) {
        if (deletedIds.current.has(id)) continue
        const remote = merged.get(id)
        if (!remote || timestamp(local.updatedAt) > timestamp(remote.updatedAt)) {
          merged.set(id, { ...local, title: remote?.title || local.title })
        }
      }
      setThreads(sortThreads([...merged.values()]))
    } catch (caught) {
      if (!disposed.current && current === request.current) setError(caught instanceof Error ? `加载研究记录失败：${caught.message}` : '加载研究记录失败')
    }
  }, [identity.tenantId, identity.userId, identity.accessToken])

  useEffect(() => {
    disposed.current = false
    const timer = window.setTimeout(() => { void refresh() }, 0)
    const onFocus = () => { void refresh() }
    window.addEventListener('focus', onFocus)
    return () => { disposed.current = true; request.current++; clearTimeout(timer); window.removeEventListener('focus', onFocus) }
  }, [refresh])

  const activateThread = useCallback((id: string) => {
    setNavigationTarget(undefined)
    saveConversationSession(window.localStorage, identity, id)
    activeId.current = id
    setActiveThreadId(id)
  }, [identity.tenantId, identity.userId])

  const navigateToMessage = useCallback((result: MessageSearchMatch) => {
    if (deletedIds.current.has(result.session_id)) return
    searchedThreads.current.set(result.session_id, {
      id: result.session_id, title: plainText(result.title, 80) || '新建投资研究',
      preview: plainText(result.snippet, 160), updatedAt: result.created_at
    })
    activateThread(result.session_id)
    setNavigationTarget({ sessionId: result.session_id, messageId: result.message_id, sequenceNo: result.sequence_no, requestId: crypto.randomUUID() })
  }, [activateThread])

  const createThread = useCallback(() => {
    const id = newSessionId()
    activateThread(id)
    return id
  }, [activateThread])

  const deleteThread = useCallback(async (id: string) => {
    await deleteResearchSession(id, identity)
    if (disposed.current) return
    deletedIds.current.add(id)
    localUpdates.current.delete(id)
    remoteThreads.current.delete(id)
    searchedThreads.current.delete(id)
    const remaining = visibleThreads.current.filter((thread) => thread.id !== id)
    setThreads(remaining)
    if (activeId.current === id) activateThread(remaining[0]?.id || newSessionId())
  }, [identity.tenantId, identity.userId, identity.accessToken, activateThread])

  // 本地提交时即时展示问题；恢复历史和完成回答的标题、时间由服务端决定。
  const updateConversation = useCallback((id: string, messages: ResearchMessage[]) => {
    if (deletedIds.current.has(id)) return
    const firstQuestion = messages.find((item) => item.role === 'user' && item.content.trim())
    const latest = [...messages].reverse().find((item) => item.role === 'user' && item.content.trim())
    if (!firstQuestion || !latest) return
    const remote = remoteThreads.current.get(id) || searchedThreads.current.get(id)
    const previous = localUpdates.current.get(id)
    const thread: ResearchThread = {
      id, title: remote?.title || previous?.title || plainText(firstQuestion.content, 80),
      preview: plainText(latest.content, 160), updatedAt: latest.createdAt
    }
    if (previous?.title === thread.title && previous.preview === thread.preview && previous.updatedAt === thread.updatedAt) return
    localUpdates.current.set(id, thread)
    if (remote && timestamp(remote.updatedAt) >= timestamp(thread.updatedAt)) return
    setThreads((current) => {
      return sortThreads([...current.filter((item) => item.id !== id), thread])
    })
  }, [])

  const localActive = localUpdates.current.get(activeThreadId)
  const remoteActive = remoteThreads.current.get(activeThreadId)
  const activeThread = threads.find((thread) => thread.id === activeThreadId)
    || (localActive && (!remoteActive || timestamp(localActive.updatedAt) > timestamp(remoteActive.updatedAt)) ? localActive : remoteActive)
    || searchedThreads.current.get(activeThreadId)

  return {
    threads, activeThread, navigationTarget, navigateToMessage, deleteThread,
    activeThreadId, activateThread, createThread, updateConversation, refresh, error
  }
}

function newSessionId() { return `session_${crypto.randomUUID()}` }
function timestamp(value: string) { return new Date(value).getTime() || 0 }
function sortThreads(threads: ResearchThread[]) { return threads.sort((a, b) => timestamp(b.updatedAt) - timestamp(a.updatedAt)) }
function plainText(value: string, limit: number) {
  return value.replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[#*`>|]/g, '').replace(/\s+/g, ' ').trim().slice(0, limit)
}
