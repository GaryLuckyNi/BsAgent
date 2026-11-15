// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Reasoning, ReasoningContent, ReasoningTrigger } from './reasoning'

vi.mock('streamdown', () => ({ Streamdown: ({ children }: { children: string }) => <div>{children}</div> }))
vi.mock('@streamdown/cjk', () => ({ cjk: {} }))
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('Reasoning', () => {
  it('流式输出期间允许用户主动收起，后续增量不会强制展开', () => {
    const view = render(<Reasoning isStreaming><ReasoningTrigger /><ReasoningContent>第一段</ReasoningContent></Reasoning>)
    const trigger = screen.getByRole('button')
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    view.rerender(<Reasoning isStreaming><ReasoningTrigger /><ReasoningContent>第一段，第二段</ReasoningContent></Reasoning>)
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

  it('思考结束后计时固定，正文继续生成不延长思考时间', () => {
    vi.useFakeTimers()
    const view = render(<Reasoning isStreaming><ReasoningTrigger /></Reasoning>)
    act(() => { vi.advanceTimersByTime(3000) })
    view.rerender(<Reasoning isStreaming={false}><ReasoningTrigger /></Reasoning>)
    expect(screen.getByText('已思考 3 秒')).toBeTruthy()
    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.getByText('已思考 3 秒')).toBeTruthy()
  })

  it('关闭自动收起后，最终答案出现时继续展示思考内容', () => {
    vi.useFakeTimers()
    const view = render(
      <Reasoning isStreaming autoClose={false}>
        <ReasoningTrigger />
        <ReasoningContent>真实思考</ReasoningContent>
      </Reasoning>
    )

    view.rerender(
      <Reasoning isStreaming={false} autoClose={false}>
        <ReasoningTrigger />
        <ReasoningContent>真实思考</ReasoningContent>
      </Reasoning>
    )
    act(() => { vi.advanceTimersByTime(1500) })

    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('真实思考')).toBeTruthy()
  })
})
