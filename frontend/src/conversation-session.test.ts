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
  it.each(['pending', 'superseded'])('刷新后保留研究活动的 %s 状态', (status) => {
    const restored = toConversationMessages([{
      role: 'assistant', status: 'completed', content: '结论', sequence_no: 1,
      metadata: { research_process: [{ id: 'skill_agent:report', kind: 'activity',
        label: '研究流程', message: '研究流程已调整', status, sequence: 2 }] }
    }])
    expect(restored[0].researchProcess?.[0]).toMatchObject({ status, sequence: 2 })
  })

  it('恢复直接来自目标和任务字段的研究过程', () => {
    const restored = toConversationMessages([{
      role: 'assistant', status: 'completed', content: '结论', sequence_no: 1,
      metadata: { research_process: [
        { id: 'goal', kind: 'goal', label: '研究目标', message: '分析行情', status: 'completed', sequence: 0 },
        { id: 'plan', kind: 'plan', label: '研究计划', message: '1. 取得行情\n2. 核验消息', status: 'completed', sequence: 1 }
      ] }
    }])
    expect(restored[0].researchProcess?.map((event) => event.kind)).toEqual(['goal', 'plan'])
    expect(restored[0].researchProcess?.[1].message).toBe('1. 取得行情\n2. 核验消息')
  })

  it('恢复空的运行中与已停止回答，保留持久化标识和状态', () => {
    const restored = toConversationMessages([
      { message_id: 'a1', run_id: 'r1', role: 'assistant', status: 'pending', content: '', sequence_no: 2, metadata: {} },
      { message_id: 'a2', run_id: 'r2', role: 'assistant', status: 'cancelled', content: '部分回答', sequence_no: 4, metadata: {} }
    ])
    expect(restored).toEqual([
      expect.objectContaining({ messageId: 'a1', runId: 'r1', status: 'pending', content: '' }),
      expect.objectContaining({ messageId: 'a2', runId: 'r2', status: 'cancelled', content: '部分回答' })
    ])
  })

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
