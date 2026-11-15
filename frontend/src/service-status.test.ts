import { describe, expect, it } from 'vitest'

import { invocationBlockedReason } from './service-status'


describe('Agent 服务状态', () => {
  it('后端离线时阻止发送并返回明确原因', () => {
    expect(invocationBlockedReason('offline')).toBe(
      'Agent 服务未启动或不可达，请先启动后端服务。'
    )
  })

  it('后端在线时允许发送', () => {
    expect(invocationBlockedReason('online')).toBeNull()
  })
})
