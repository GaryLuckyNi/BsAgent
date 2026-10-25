// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it } from 'vitest'

import { PermissionRecoveryItems } from './permission-recovery-items'

afterEach(cleanup)

it('展开详情后展示全部十二项授权范围，不截断后面的任务', async () => {
  const items = Array.from({ length: 12 }, (_, i) => ({
    item_id: `c${i}`, task_goal: `目标${i}`, original_tool: `mcp.x.api${i}`,
    reason: '上游接口权限不足', source_notice: '公开来源不等价于原接口数据'
  }))
  render(<PermissionRecoveryItems items={items} />)
  await userEvent.click(screen.getByText('查看详情（12项任务）'))
  expect(screen.getAllByRole('listitem')).toHaveLength(12)
  expect(screen.getByText('目标11')).toBeTruthy()
})

it.each([
  ['历史工具执行尚未核实，本次原接口未派发。', '原数据源暂不可用：先前查询的状态尚未确认。'],
  ['数据源账号权限不足，原接口未执行。', '当前账号没有该数据源的访问权限。'],
  ['未来新增的失败原因', '原数据源暂不可用，未取得所需数据。']
])('默认简述原因，不展开完整任务或内部接口：%s', (reason, summary) => {
  const { container } = render(<PermissionRecoveryItems items={[{
    item_id: 'one', task_goal: '完整的多步骤研究目标', original_tool: 'mcp.tushare.limit_list_d',
    reason, source_notice: '完整的授权限制和来源声明'
  }]} />)
  expect(screen.getByText(summary)).toBeTruthy()
  const details = container.querySelector('details')!
  expect(details).toBeTruthy()
  expect(details.open).toBe(false)
  expect(details.textContent).toContain('完整的多步骤研究目标')
  expect(details.textContent).toContain('mcp.tushare.limit_list_d')
  expect(details.textContent).toContain('完整的授权限制和来源声明')
})

it('同批原因去重，混合原因分别简述', () => {
  render(<PermissionRecoveryItems items={[
    { item_id: 'a', task_goal: '任务A', original_tool: 'mcp.x.a', reason: '数据源账号权限不足，原接口未执行。', source_notice: '声明' },
    { item_id: 'b', task_goal: '任务B', original_tool: 'mcp.x.b', reason: '数据源账号权限不足，原接口未执行。', source_notice: '声明' },
    { item_id: 'c', task_goal: '任务C', original_tool: 'mcp.x.c', reason: '历史工具执行尚未核实，本次原接口未派发。', source_notice: '声明' }
  ]} />)
  expect(screen.getAllByText('当前账号没有该数据源的访问权限。')).toHaveLength(1)
  expect(screen.getAllByText('原数据源暂不可用：先前查询的状态尚未确认。')).toHaveLength(1)
})
