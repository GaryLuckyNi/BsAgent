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
  ReasoningTrigger: ({ getThinkingMessage }: { getThinkingMessage?: (streaming: boolean, duration?: number) => ReactNode }) => <div>{getThinkingMessage?.(false, 3) || '研究过程'}</div>,
  ReasoningContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ReasoningText: ({ children }: { children: ReactNode }) => <div>{children}</div>
}))

describe('ResearchMessageItem', () => {
  it('正文流式输出期间隐藏来源入口，完整回答结束后再展示', () => {
    const message = { id: 'stream-sources', role: 'assistant' as const, content: '已输出的正文', createdAt: '',
      status: 'streaming' as const, evidence: [{ id: 'source_7', index: 7, title: '报告', url: 'https://example.com/report' }] }
    const view = render(<ResearchMessageItem message={message} onCitation={vi.fn()} />)
    expect(screen.getByText('已输出的正文')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /研究来源/ })).toBeNull()
    view.rerender(<ResearchMessageItem message={{ ...message, status: 'complete', content: '完整正文' }} onCitation={vi.fn()} />)
    expect(screen.getByText('完整正文')).toBeTruthy()
    expect(screen.getByRole('button', { name: /研究来源/ })).toBeTruthy()
  })

  it('没有正文时不单独展示来源入口', () => {
    render(<ResearchMessageItem message={{ id: 'empty-sources', role: 'assistant', content: '  ', createdAt: '',
      status: 'complete', evidence: [{ id: 'source_7', index: 7, title: '报告', url: 'https://example.com/report' }] }} onCitation={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /研究来源/ })).toBeNull()
  })

  it('已替代的查询不计入已取得数据的数量', () => {
    render(<ResearchMessageItem message={{ id: 'group', role: 'assistant', content: '结论', createdAt: '',
      status: 'complete', progress: [{ id: 'market', kind: 'activity', label: '市场数据',
        message: '已调整流程', status: 'completed', activityType: 'market_data', queryCount: 2,
        queries: [
          { taskId: 'ok', query: '成功', status: 'completed', resultUrls: [] },
          { taskId: 'replaced', query: '替代', status: 'superseded', resultUrls: [] }
        ] }] }} onCitation={vi.fn()} />)
    expect(screen.getByText('已取得 1 项市场数据')).toBeTruthy()
    expect(screen.queryByText('已取得 2 项市场数据')).toBeNull()
  })

  it('待补偿及已替代的流程使用中性图标，只有最终失败显示感叹号', () => {
    const message = { id: 'replan', role: 'assistant' as const, content: '', createdAt: '',
      status: 'streaming' as const, progress: [{ id: 'skill', kind: 'activity' as const,
        label: '研究流程', message: '正在评估后续方案', status: 'pending' as const }] }
    const view = render(<ResearchMessageItem message={message} onCitation={vi.fn()} />)
    expect(view.container.querySelector('.lucide-circle-alert')).toBeNull()
    view.rerender(<ResearchMessageItem message={{ ...message, progress: [{ ...message.progress[0],
      status: 'superseded', message: '已调整研究流程' }] }} onCitation={vi.fn()} />)
    expect(screen.getByText('已调整研究流程')).toBeTruthy()
    expect(view.container.querySelector('.lucide-workflow')).toBeTruthy()
    expect(view.container.querySelector('.lucide-circle-alert')).toBeNull()
    view.rerender(<ResearchMessageItem message={{ ...message, progress: [{ ...message.progress[0],
      status: 'failed', message: '研究流程未完成' }] }} onCitation={vi.fn()} />)
    expect(view.container.querySelector('.lucide-circle-alert')).toBeTruthy()
  })

  it('历史消息没有可靠计时数据时，不使用组件挂载时长作为思考用时', () => {
    render(<ResearchMessageItem message={{
      id: 'history', role: 'assistant', content: '历史答案', status: 'complete', createdAt: '2026-10-28T00:00:00Z',
      progress: [{ id: 'goal', kind: 'goal', label: '研究目标', message: '目标', status: 'completed' }]
    }} onCitation={vi.fn()} />)
    expect(screen.getByText('研究进展')).toBeTruthy()
    expect(screen.queryByText(/思考用时|· 3 秒/)).toBeNull()
  })
  it('搜索完成即在 timeline 下方展示摘要，其他并行搜索仍可继续', () => {
    const message = {
      id: 'answers', role: 'assistant' as const, content: '', createdAt: '', status: 'streaming' as const,
      progress: [{
        id: 'web', kind: 'activity' as const, label: '获取证据', message: '搜索中',
        activityType: 'web_search' as const, status: 'started' as const, queryCount: 2,
        queries: [
          { taskId: 'a', query: '量价', status: 'completed' as const, resultUrls: [], answer: '量价摘要' },
          { taskId: 'b', query: '资金', status: 'started' as const, resultUrls: [], answer: '未完成不展示' }
        ]
      }]
    }
    const view = render(<ResearchMessageItem message={message} onCitation={vi.fn()} />)
    expect(screen.getByText('正在搜索 2 项信息…')).toBeTruthy()
    const summaries = screen.getByRole('region', { name: '搜索摘要' })
    expect(summaries.textContent).toContain('量价摘要')
    expect(summaries.textContent).not.toContain('未完成不展示')
    const timeline = view.container.querySelector('.reasoning-stream')!
    expect(timeline.compareDocumentPosition(summaries) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    view.rerender(<ResearchMessageItem message={{ ...message, progress: [{ ...message.progress[0],
      queries: message.progress[0].queries.map((query) => ({ ...query,
        status: 'completed' as const, answer: query.taskId === 'a' ? '量价摘要' : '资金摘要' }))
    }] }} onCitation={vi.fn()} />)
    expect(summaries.textContent).toContain('量价摘要')
    expect(summaries.textContent).toContain('资金摘要')
  })

  it('没有非空搜索 answer 时不显示摘要区域', () => {
    render(<ResearchMessageItem message={{
      id: 'empty', role: 'assistant', content: '', createdAt: '', status: 'streaming',
      progress: [{ id: 'web', kind: 'activity', label: '获取证据', message: '完成',
        activityType: 'web_search', status: 'completed', queries: [
          { taskId: 'a', query: '查询', status: 'completed', resultUrls: [], answer: ' ' }
        ] }]
    }} onCitation={vi.fn()} />)
    expect(screen.queryByRole('region', { name: '搜索摘要' })).toBeNull()
    expect(screen.queryByText('查看搜索范围')).toBeNull()
  })

  it('直接展示研究目标和任务列表，不需要模型生成摘要', () => {
    render(<ResearchMessageItem message={{
      id: 'goal-plan', role: 'assistant', content: '', createdAt: '', status: 'streaming',
      progress: [
        { id: 'goal', kind: 'goal', label: '研究目标', message: '分析当日表现', status: 'completed', sequence: 0 },
        { id: 'plan', kind: 'plan', label: '研究计划', message: '1. 核验行情\n2. 核验消息', status: 'completed', sequence: 1 },
        { id: 'task', kind: 'activity', label: '获取证据', message: '正在核验行情', status: 'started', sequence: 2 }
      ]
    }} onCitation={vi.fn()} />)
    expect(screen.getByText('研究目标')).toBeTruthy()
    expect(screen.getByText('分析当日表现')).toBeTruthy()
    expect(screen.getByText('研究计划')).toBeTruthy()
    expect(screen.getByText(/1\. 核验行情/)).toBeTruthy()
    expect(screen.getByText('正在核验行情')).toBeTruthy()
  })

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

  it('搜索分组仅展示网页总数，不重复展示研究计划中的查询范围', () => {
    render(<ResearchMessageItem message={{
      id: 'search-group', role: 'assistant', createdAt: new Date().toISOString(),
      status: 'complete', content: '完成', thinkingDurationSeconds: 12,
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

    expect(screen.getByText('研究进展')).toBeTruthy()
    expect(screen.getByText('· 思考用时 12 秒')).toBeTruthy()
    expect(screen.getByText('搜索到 3 个网页')).toBeTruthy()
    expect(screen.queryByText('查看搜索范围')).toBeNull()
    expect(screen.queryByText('搜索量价')).toBeNull()
    expect(screen.queryByText('搜索资金')).toBeNull()
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
    expect(screen.queryByText('查看任务明细')).toBeNull()
    expect(screen.queryByText('查询当日行情')).toBeNull()
    expect(screen.queryByText('查询资金流向')).toBeNull()
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
