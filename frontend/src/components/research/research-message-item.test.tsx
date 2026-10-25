// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ResearchMessageItem } from './research-message-item'

afterEach(cleanup)

vi.mock('@/components/ai-elements/message', () => ({
  Message: ({ children }: { children: ReactNode }) => <article>{children}</article>,
  MessageContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MessageResponse: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MessageActions: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MessageAction: ({ children }: { children: ReactNode }) => <button>{children}</button>
}))

vi.mock('@/components/ai-elements/reasoning', () => ({
  Reasoning: ({ children, isStreaming }: { children: ReactNode; isStreaming: boolean }) => <section data-testid="reasoning" data-streaming={isStreaming}>{children}</section>,
  ReasoningTrigger: () => <div>研究过程</div>,
  ReasoningContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ReasoningText: ({ children }: { children: ReactNode }) => <div>{children}</div>
}))

describe('ResearchMessageItem', () => {
  it('恢复卡片使用简短问题和选项，点击仍传原始授权值', () => {
    const onUserInputSelect = vi.fn()
    render(<ResearchMessageItem message={{
      id: 'compact-recovery', role: 'assistant', content: '', createdAt: '', status: 'interrupted',
      userInput: {
        interruptId: 'existing-interrupt', interactionId: 'existing-interaction', inputType: 'selection',
        question: '以下任务因先前工具执行尚未核实等原因未取得原数据，是否改用联网搜索补充公开资料？',
        options: [
          { value: 'web_search', label: '对以上任务使用联网搜索', description: '只对清单中的任务替代取证；公开来源不等价于原接口数据。' },
          { value: 'use_existing', label: '仅使用已有资料', description: '不搜索，保留原接口数据缺口。' }
        ],
        recoveryBatch: {
          version: 'permission-recovery-batch-v1', batch_id: 'existing-batch', digest: 'a'.repeat(64),
          items: [{ item_id: 'existing-call', task_goal: '完整研究任务', original_tool: 'mcp.tushare.limit_list_d',
            reason: '历史工具执行尚未核实，本次原接口未派发。', source_notice: '历史记录与来源限制' }]
        }
      }
    }} onCitation={vi.fn()} onUserInputSelect={onUserInputSelect} />)
    expect(screen.getByRole('heading', { name: '是否改用联网搜索补充公开资料？' })).toBeTruthy()
    expect(screen.queryByText('不搜索，保留原接口数据缺口。')).toBeNull()
    screen.getByRole('button', { name: '使用联网搜索' }).click()
    expect(onUserInputSelect).toHaveBeenLastCalledWith('compact-recovery', 'web_search')
    screen.getByRole('button', { name: '仅用已有资料' }).click()
    expect(onUserInputSelect).toHaveBeenLastCalledWith('compact-recovery', 'use_existing')
  })

  it('批次展示不兼容时显示提示且禁止两个选项', () => {
    render(<ResearchMessageItem message={{
      id: 'batch', role: 'assistant', content: '', createdAt: '', status: 'interrupted',
      userInput: {
        interruptId: 'interrupt', interactionId: 'interaction', inputType: 'selection', question: '确认？',
        options: [{ value: 'web_search', label: '搜索' }, { value: 'use_existing', label: '已有资料' }],
        presentationError: '请更新页面后重新发起'
      }
    }} onCitation={vi.fn()} />)
    expect(screen.getByRole('alert').textContent).toBe('请更新页面后重新发起')
    expect((screen.getByRole('button', { name: '搜索' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: '已有资料' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('停止的部分回答明确标记不完整，空回答也显示停止状态', () => {
    render(<ResearchMessageItem message={{ id: 'a1', role: 'assistant', content: '', createdAt: '', status: 'cancelled' }} onCitation={vi.fn()} />)
    expect(screen.getByText('已停止 · 回答可能不完整')).toBeTruthy()
    expect(screen.queryByText('研究完成')).toBeNull()
    expect(screen.queryByLabelText('回复生成中')).toBeNull()
  })
  it('等待首个公开研究事件时显示 loading 动画，收到摘要后直接展示', () => {
    const message = {
      id: 'direct', role: 'assistant' as const, content: '', createdAt: '',
      status: 'streaming' as const, processStatus: '正在规划…'
    }
    const view = render(<ResearchMessageItem message={message} onCitation={vi.fn()} />)
    expect(screen.queryByTestId('reasoning')).toBeNull()
    expect(screen.getByLabelText('回复生成中')).toBeTruthy()
    expect(view.container.querySelector('.response-stream-loader')).toBeTruthy()
    expect(screen.queryByText(/正在规划|正在分析/)).toBeNull()
    view.rerender(<ResearchMessageItem message={{ ...message, progress: [
      {
        id: 'plan', label: '研究计划', message: '先核验市场数据。',
        status: 'completed', kind: 'summary'
      }
    ] }} onCitation={vi.fn()} />)
    expect(screen.queryByLabelText('回复生成中')).toBeNull()
    expect(screen.getByText('先核验市场数据。')).toBeTruthy()
    expect(screen.queryByText('研究过程')).toBeNull()
    expect(screen.queryByText('正在规划…')).toBeNull()
  })

  it('研究过程忽略原始思考并展示当前工具活动', () => {
    render(
      <ResearchMessageItem
        message={{
          id: 'assistant-1',
          role: 'assistant',
          content: '',
          createdAt: new Date().toISOString(),
          status: 'streaming',
          thinking: '我会先确认分析范围，再根据证据得出结论。',
          progress: [
            {
              id: 'tool:1',
              stage: 'tool_agent',
              label: 'web_search',
              message: '正在查询市场数据：获取最新行情',
              status: 'started'
            }
          ]
        }}
        onCitation={vi.fn()}
      />
    )

    expect(screen.queryByText('我会先确认分析范围，再根据证据得出结论。')).toBeNull()
    expect(screen.getByText('正在查询市场数据：获取最新行情')).toBeTruthy()
    expect(screen.queryByText('web_search')).toBeNull()
  })

  it('不展示遗留原始推理中的内部协议', () => {
    render(
      <ResearchMessageItem
        message={{
          id: 'assistant-2',
          role: 'assistant',
          content: '',
          createdAt: new Date().toISOString(),
          status: 'streaming',
          reasoning: [
            {
              id: 'planner:1',
              label: '制定计划',
              content: '检查 allowed_taxonomy 和 skill_name。'
            },
            {
              id: 'analysis:1',
              label: '分析证据',
              content: '按照 required_schema 输出 JSON object。'
            }
          ]
        }}
        onCitation={vi.fn()}
      />
    )

    expect(screen.queryByText(/allowed_taxonomy/)).toBeNull()
    expect(screen.queryByText(/required_schema/)).toBeNull()
    expect(screen.queryByTestId('reasoning')).toBeNull()
  })

  it('没有推理增量时仍展示当前研究活动', () => {
    render(
      <ResearchMessageItem
        message={{
          id: 'assistant-3',
          role: 'assistant',
          content: '',
          createdAt: new Date().toISOString(),
          status: 'streaming',
          progress: [{
            id: 'planner',
            stage: 'tool_agent',
            label: '制定计划',
            message: '正在联网检索：核验近期政策与市场动态',
            status: 'started'
          }]
        }}
        onCitation={vi.fn()}
      />
    )

    expect(screen.queryByText('研究过程')).toBeNull()
    expect(screen.getByText('正在联网检索：核验近期政策与市场动态')).toBeTruthy()
    expect(screen.queryByText('正在整理研究结果…')).toBeNull()
  })

  it('收到首个答案内容后才渲染答案区域', () => {
    const { rerender } = render(
      <ResearchMessageItem
        message={{
          id: 'assistant-4',
          role: 'assistant',
          content: '',
          reasoning: [{ id: 'intent', label: '理解问题', content: '先确认投研范围。' }],
          createdAt: new Date().toISOString(),
          status: 'streaming'
        }}
        onCitation={vi.fn()}
      />
    )

    expect(screen.queryByText('正在整理研究结果…')).toBeNull()

    rerender(
      <ResearchMessageItem
        message={{
          id: 'assistant-4',
          role: 'assistant',
          content: '## 结论',
          reasoning: [{ id: 'intent', label: '理解问题', content: '先确认投研范围。' }],
          createdAt: new Date().toISOString(),
          status: 'streaming'
        }}
        onCitation={vi.fn()}
      />
    )

    expect(screen.getByText('## 结论')).toBeTruthy()
  })
  it('工具活动展示在两段公开摘要之间，答案开始后停止流式状态', () => {
    const { container } = render(<ResearchMessageItem message={{
      id: 'timeline', role: 'assistant', createdAt: new Date().toISOString(), status: 'streaming',
      content: '最终答案',
      progress: [
        { id: 'plan', label: '研究计划', message: '先搜索', status: 'completed', kind: 'summary', sequence: 0 },
        { id: 'tool', stage: 'tool_agent', label: '工具', message: '已完成联网检索', status: 'completed', kind: 'activity', sequence: 1 },
        { id: 'answer', label: '研究发现', message: '再综合', status: 'completed', kind: 'summary', sequence: 2 }
      ]
    }} onCitation={vi.fn()} />)
    const text = container.textContent!
    expect(text.indexOf('先搜索')).toBeLessThan(text.indexOf('已完成联网检索'))
    expect(text.indexOf('已完成联网检索')).toBeLessThan(text.indexOf('再综合'))
    expect(screen.getByTestId('reasoning').getAttribute('data-streaming')).toBe('false')
  })

  it('将搜索分组渲染为网页总数并保留查询方向', () => {
    render(<ResearchMessageItem message={{
      id: 'search-group', role: 'assistant', createdAt: new Date().toISOString(),
      status: 'complete', content: '完成',
      progress: [{
        id: 'web_search:1', label: '获取证据', message: '已完成联网检索',
        status: 'completed', kind: 'activity', activityType: 'web_search',
        groupId: 'web_search:1', queryCount: 2, resultCount: 3,
        queries: [
          { taskId: 'a', query: '搜索量价', status: 'completed', resultUrls: [] },
          { taskId: 'b', query: '搜索资金', status: 'completed', resultUrls: [] }
        ]
      }]
    }} onCitation={vi.fn()} />)

    expect(screen.getByText('搜索到 3 个网页')).toBeTruthy()
    expect(screen.getByText('搜索量价')).toBeTruthy()
    expect(screen.getByText('搜索资金')).toBeTruthy()
  })

  it('将相同类型的市场数据活动合并为一个操作组', () => {
    render(<ResearchMessageItem message={{
      id: 'market-group', role: 'assistant', createdAt: new Date().toISOString(),
      status: 'streaming', content: '',
      progress: [{
        id: 'market_data:1', label: '获取证据', message: '已取得市场数据',
        status: 'completed', kind: 'activity', activityType: 'market_data' as never,
        queryCount: 2,
        queries: [
          { taskId: 'price', query: '查询当日行情', status: 'completed', resultUrls: [] },
          { taskId: 'flow', query: '查询资金流向', status: 'completed', resultUrls: [] }
        ]
      }]
    }} onCitation={vi.fn()} />)

    expect(screen.getByText('已取得 2 项市场数据')).toBeTruthy()
    expect(screen.getByText('查询当日行情')).toBeTruthy()
    expect(screen.getByText('查询资金流向')).toBeTruthy()
  })

  it('相同的模型思考和结构化摘要只展示一次', () => {
    render(<ResearchMessageItem message={{
      id: 'dedupe', role: 'assistant', createdAt: new Date().toISOString(),
      status: 'streaming', content: '',
      reasoning: [{ id: 'planner:reasoning', label: '制定计划', content: '先核对公告，再比较行情。', sequence: 0 }],
      progress: [{
        id: 'planner:0', label: '研究计划', message: '先核对公告，再比较行情。',
        status: 'completed', kind: 'summary', sequence: 1
      }]
    }} onCitation={vi.fn()} />)

    expect(screen.getAllByText('先核对公告，再比较行情。')).toHaveLength(1)
  })

  it('相同的结构化摘要在时间线中只展示一次', () => {
    render(<ResearchMessageItem message={{
      id: 'summary-dedupe', role: 'assistant', createdAt: new Date().toISOString(),
      status: 'complete', content: '完成',
      progress: [
        {
          id: 'planner:summary', label: '研究计划',
          message: '先核实涨停区间，再核验公告。',
          status: 'completed', kind: 'summary', sequence: 0
        },
        {
          id: 'analysis:summary', label: '研究发现',
          message: '先核实涨停区间，再核验公告。',
          status: 'completed', kind: 'summary', sequence: 2
        }
      ]
    }} onCitation={vi.fn()} />)

    expect(screen.getAllByText('先核实涨停区间，再核验公告。')).toHaveLength(1)
  })

  it('渲染通用 selection 卡片并提交选中值', async () => {
    const onUserInputSelect = vi.fn()
    render(<ResearchMessageItem message={{
      id: 'selection', role: 'assistant', createdAt: new Date().toISOString(),
      status: 'interrupted', content: '',
      userInput: {
        interruptId: 'interrupt-1', interactionId: 'interaction-1',
        inputType: 'selection', question: '请选择证券标的',
        options: [
          { value: '688825.SH', label: 'C长鑫', description: '证券代码：688825.SH' },
          { value: '603986.SH', label: '兆易创新' }
        ]
      }
    }} onCitation={vi.fn()} onUserInputSelect={onUserInputSelect} />)

    expect(screen.getByText('请选择证券标的')).toBeTruthy()
    expect(screen.getByText('证券代码：688825.SH')).toBeTruthy()
    screen.getByRole('button', { name: /C长鑫/ }).click()
    expect(onUserInputSelect).toHaveBeenCalledWith('selection', '688825.SH')
  })

  it('恢复中禁用所有 selection 选项', () => {
    render(<ResearchMessageItem message={{
      id: 'selection-loading', role: 'assistant', createdAt: '',
      status: 'interrupted', content: '',
      userInput: {
        interruptId: 'interrupt-1', interactionId: 'interaction-1',
        inputType: 'selection', question: '请选择', isSubmitting: true,
        options: [
          { value: 'a', label: 'A' },
          { value: 'b', label: 'B' }
        ]
      }
    }} onCitation={vi.fn()} onUserInputSelect={vi.fn()} />)

    expect(screen.getAllByRole('button', { name: /A|B/ }))
      .toSatisfy((buttons: HTMLButtonElement[]) => buttons.every((button) => button.disabled))
  })

})
