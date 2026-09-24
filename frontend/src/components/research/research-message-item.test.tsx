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
  it('思考面板在模型推理后展示当前工具活动', () => {
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

    expect(screen.getByText('我会先确认分析范围，再根据证据得出结论。')).toBeTruthy()
    expect(screen.getByText('正在查询市场数据：获取最新行情')).toBeTruthy()
    expect(screen.queryByText('web_search')).toBeNull()
  })

  it('多次模型推理保留段落边界且不暴露节点标签', () => {
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
              content: '我会先拆分需要核对的投研维度。'
            },
            {
              id: 'analysis:1',
              label: '分析证据',
              content: '已获得数据，需要区分事实与推断。'
            }
          ]
        }}
        onCitation={vi.fn()}
      />
    )

    expect(screen.queryByText('制定计划')).toBeNull()
    expect(screen.queryByText('分析证据')).toBeNull()
    expect(screen.getByText('我会先拆分需要核对的投研维度。')).toBeTruthy()
    expect(screen.getByText('已获得数据，需要区分事实与推断。')).toBeTruthy()
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

    expect(screen.getByText('研究过程')).toBeTruthy()
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
  it('工具活动展示在两段推理之间，答案开始后不再显示正在思考', () => {
    const { container } = render(<ResearchMessageItem message={{
      id: 'timeline', role: 'assistant', createdAt: new Date().toISOString(), status: 'streaming',
      content: '最终答案',
      reasoning: [
        { id: 'plan', label: '计划', content: '先搜索', sequence: 0 },
        { id: 'answer', label: '生成回答', content: '再综合', sequence: 2 }
      ],
      progress: [{ id: 'tool', stage: 'tool_agent', label: '工具', message: '已完成联网检索', status: 'completed', sequence: 1 }]
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

})
