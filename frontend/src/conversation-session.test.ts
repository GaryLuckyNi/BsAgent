import { describe, expect, it } from 'vitest'

import {
  resolveConversationSession,
  toConversationMessages,
  type ConversationSessionStorage
} from './conversation-session'

function memoryStorage(): ConversationSessionStorage {
  const values = new Map<string, string>()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value)
  }
}

describe('resolveConversationSession', () => {
  it('刷新后为同一租户和用户复用已保存的会话', () => {
    const storage = memoryStorage()
    const createSessionId = () => 'session_new'

    const first = resolveConversationSession(
      storage,
      { tenantId: 'tenant_1', userId: 'user_1' },
      createSessionId
    )
    const restored = resolveConversationSession(
      storage,
      { tenantId: 'tenant_1', userId: 'user_1' },
      () => 'session_should_not_be_used'
    )

    expect(first).toBe('session_new')
    expect(restored).toBe('session_new')
  })

  it('不同用户不会共享会话标识', () => {
    const storage = memoryStorage()

    const first = resolveConversationSession(
      storage,
      { tenantId: 'tenant_1', userId: 'user_1' },
      () => 'session_user_1'
    )
    const second = resolveConversationSession(
      storage,
      { tenantId: 'tenant_1', userId: 'user_2' },
      () => 'session_user_2'
    )

    expect(first).toBe('session_user_1')
    expect(second).toBe('session_user_2')
  })
})

describe('toConversationMessages', () => {
  it('按顺序把持久化的用户和助手消息恢复为前端消息', () => {
    expect(
      toConversationMessages([
        {
          role: 'assistant',
          status: 'completed',
          content: '分析结果',
          sequence_no: 2,
          metadata: {
            trace_id: 'trace_1',
            approval_status: 'not_required',
            cited_evidence_ids: ['evidence_1'],
            research_process: [
              {
                id: 'planner:reasoning',
                kind: 'reasoning',
                label: '制定计划',
                content: '先核验公告，再检查行情。',
                sequence: 0
              },
              {
                id: 'market:daily',
                kind: 'activity',
                label: '获取证据',
                message: '已取得市场数据',
                status: 'completed',
                sequence: 1,
                activity_type: 'market_data',
                group_id: 'market_data:1',
                task_id: 'daily',
                task_label: '查询当日行情'
              }
            ]
          }
        },
        {
          role: 'user',
          status: 'completed',
          content: '分析一下海能达',
          sequence_no: 1,
          metadata: {}
        }
      ])
    ).toEqual([
      { role: 'user', content: '分析一下海能达' },
      {
        role: 'agent',
        content: '分析结果',
        traceId: 'trace_1',
        approvalStatus: 'not_required',
        citedEvidenceIds: ['evidence_1'],
        researchProcess: [{
          id: 'market:daily',
          kind: 'activity',
          label: '获取证据',
          message: '已取得市场数据',
          status: 'completed',
          sequence: 1,
          activityType: 'market_data',
          groupId: 'market_data:1',
          taskId: 'daily',
          taskLabel: '查询当日行情'
        }]
      }
    ])
  })
})
