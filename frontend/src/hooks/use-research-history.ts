import { useCallback, useMemo, useState } from 'react'

import type { ResearchThread } from '../domain/research'

const initialThreads: ResearchThread[] = [
  { id: 'market-outlook', title: 'A 股三季度风格展望', preview: '大盘与成长风格如何切换？', updatedAt: '刚刚', pinned: true },
  { id: 'new-energy', title: '新能源产业链跟踪', preview: '上游价格与排产数据复盘', updatedAt: '昨天' },
  { id: 'bank-dividend', title: '银行股红利策略', preview: '股息率与资产质量对比', updatedAt: '8月26日' }
]

export function useResearchHistory() {
  const [threads, setThreads] = useState(initialThreads)
  const [activeThreadId, setActiveThreadId] = useState(initialThreads[0].id)
  const activeThread = useMemo(
    () => threads.find((thread) => thread.id === activeThreadId) ?? threads[0],
    [activeThreadId, threads]
  )

  const createThread = useCallback(() => {
    const thread: ResearchThread = {
      id: crypto.randomUUID(),
      title: '新建投资研究',
      preview: '等待提问',
      updatedAt: '刚刚'
    }
    setThreads((current) => [thread, ...current])
    setActiveThreadId(thread.id)
    return thread.id
  }, [])

  return { threads, activeThread, activeThreadId, activateThread: setActiveThreadId, createThread }
}
