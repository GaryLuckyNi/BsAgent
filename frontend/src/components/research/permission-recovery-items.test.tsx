// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { PermissionRecoveryItems } from './permission-recovery-items'

afterEach(cleanup)

it('展示全部十二项授权范围，不隐藏后面的任务', () => {
  const items = Array.from({ length: 12 }, (_, i) => ({
    item_id: `c${i}`, task_goal: `目标${i}`, original_tool: `mcp.x.api${i}`,
    reason: '上游接口权限不足', source_notice: '公开来源不等价于原接口数据'
  }))
  render(<PermissionRecoveryItems items={items} />)
  expect(screen.getAllByRole('listitem')).toHaveLength(12)
  expect(screen.getByText('目标11')).toBeTruthy()
})
