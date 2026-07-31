import { describe, expect, it } from 'vitest'

import {
  appendThinkingSectionDelta,
  applyAgentStreamEvent,
  finalThinkingContent,
  parseSseEvent,
  shouldSubmitTextareaKey,
  shouldUseFinalAnswerFallback,
  type AgentStreamView
} from './agent-stream'

describe('parseSseEvent', () => {
  it('parses named SSE events with JSON payloads', () => {
    expect(parseSseEvent('event: thinking_delta\ndata: {"content":"我先分析"}')).toEqual({
      event: 'thinking_delta',
      data: '{"content":"我先分析"}'
    })
  })
})

describe('applyAgentStreamEvent', () => {
  it('keeps node trace out of user-visible thinking and answer content', () => {
    const view: AgentStreamView = {
      thinking: '',
      answer: '',
      progress: null,
      traceEvents: []
    }

    applyAgentStreamEvent(view, {
      event: 'thinking_delta',
      data: '{"content":"我先拆解你的问题。"}'
    })
    applyAgentStreamEvent(view, {
      event: 'agent_event',
      data: '{"trace_id":"trace_1","node_name":"intent_router","status":"success","latency_ms":1,"input_ref":null,"output_ref":null,"error":null,"metadata":{"summary":"内部节点摘要"}}'
    })
    applyAgentStreamEvent(view, {
      event: 'answer_delta',
      data: '{"content":"最终答案。"}'
    })

    expect(view.thinking).toBe('我先拆解你的问题。')
    expect(view.answer).toBe('最终答案。')
    expect(view.traceEvents).toHaveLength(1)
    expect(view.thinking).not.toContain('intent_router')
    expect(view.answer).not.toContain('内部节点摘要')
  })

  it('replaces product progress and clears it when answer starts', () => {
    const view: AgentStreamView = {
      thinking: '',
      answer: '',
      progress: null,
      traceEvents: []
    }

    applyAgentStreamEvent(view, {
      event: 'progress',
      data: '{"stage":"collecting_market_data","message":"正在获取公司、行情与财务数据"}'
    })
    expect(view.progress).toEqual({
      stage: 'collecting_market_data',
      message: '正在获取公司、行情与财务数据'
    })

    applyAgentStreamEvent(view, {
      event: 'answer_delta',
      data: '{"content":"核心判断"}'
    })
    expect(view.progress).toBeNull()
    expect(view.answer).toBe('核心判断')
  })

  it('groups thinking deltas by public stage metadata', () => {
    const view: AgentStreamView = {
      thinking: '',
      thinkingSections: [],
      answer: '',
      progress: null,
      traceEvents: []
    }

    applyAgentStreamEvent(view, {
      event: 'thinking_delta',
      data: '{"stage":"planning","label":"规划","content":"我先确认目标。"}'
    })
    applyAgentStreamEvent(view, {
      event: 'thinking_delta',
      data: '{"stage":"tool_repair","label":"实体修复","content":"再修正实体。"}'
    })
    applyAgentStreamEvent(view, {
      event: 'thinking_delta',
      data: '{"stage":"tool_repair","label":"实体修复","content":"并验证标的。"}'
    })

    expect(view.thinking).toBe('我先确认目标。再修正实体。并验证标的。')
    expect(view.thinkingSections).toEqual([
      { stage: 'planning', label: '规划', content: '我先确认目标。' },
      {
        stage: 'tool_repair',
        label: '实体修复',
        content: '再修正实体。并验证标的。'
      }
    ])
  })
})

describe('appendThinkingSectionDelta', () => {
  it('appends content to the latest matching stage only', () => {
    const sections = [{ stage: 'planning', label: '规划', content: '第一句。' }]

    appendThinkingSectionDelta(sections, {
      stage: 'planning',
      label: '规划',
      content: '第二句。'
    })
    appendThinkingSectionDelta(sections, {
      stage: 'answer_synthesis',
      label: '答案合成',
      content: '第三句。'
    })

    expect(sections).toEqual([
      { stage: 'planning', label: '规划', content: '第一句。第二句。' },
      { stage: 'answer_synthesis', label: '答案合成', content: '第三句。' }
    ])
  })
})

describe('shouldSubmitTextareaKey', () => {
  it.each([
    [{ key: 'Enter', shiftKey: false, isComposing: false, keyCode: 13 }, true],
    [{ key: 'Enter', shiftKey: true, isComposing: false, keyCode: 13 }, false],
    [{ key: 'Enter', shiftKey: false, isComposing: true, keyCode: 229 }, false],
    [{ key: 'a', shiftKey: false, isComposing: false, keyCode: 65 }, false]
  ])('decides whether a textarea key submits', (event, expected) => {
    expect(shouldSubmitTextareaKey(event)).toBe(expected)
  })
})

describe('shouldUseFinalAnswerFallback', () => {
  it('does not enqueue the final answer again when answer deltas were already received', () => {
    expect(
      shouldUseFinalAnswerFallback({
        receivedAnswerDelta: true,
        currentContent: '',
        finalContent: '最终答案。'
      })
    ).toBe(false)
  })

  it('uses the final answer only when the stream had no answer deltas', () => {
    expect(
      shouldUseFinalAnswerFallback({
        receivedAnswerDelta: false,
        currentContent: '',
        finalContent: '最终答案。'
      })
    ).toBe(true)
  })
})

describe('finalThinkingContent', () => {
  it('keeps received thinking content', () => {
    expect(
      finalThinkingContent({
        thinking: '我会先识别公司，再检查数据。',
        userMessage: '分析一下海能达'
      })
    ).toBe('我会先识别公司，再检查数据。')
  })

  it('returns a product fallback when no thinking tokens were received', () => {
    expect(
      finalThinkingContent({
        thinking: '',
        userMessage: '分析一下海能达'
      })
    ).toContain('我会先拆解你的问题')
  })
})
