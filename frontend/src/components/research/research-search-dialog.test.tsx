// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ResearchSearchDialog } from './research-search-dialog'

const search = vi.hoisted(() => vi.fn())
vi.mock('@/services/research-search', () => ({ searchResearchMessages: search }))
const identity = { tenantId: 't1', userId: 'u1' }
const hit = { session_id: 's1', message_id: 'a1', title: '历史研究', snippet: '这是宁德时代的历史回答', role: 'assistant' as const, sequence_no: 2, created_at: '2026-10-28T01:00:00Z' }
beforeEach(() => {
  search.mockReset().mockResolvedValue({ items: [hit], next_offset: null })
  const matches = Element.prototype.matches
  vi.spyOn(Element.prototype, 'matches').mockImplementation(function (this: Element, selector) {
    if (selector === ':modal' || selector === ':fullscreen') return false
    return matches.call(this, selector)
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('消息级研究搜索', () => {
  it('Escape 关闭后恢复搜索按钮焦点', async () => {
    function Preview() {
      const [open, setOpen] = useState(false)
      return <><button onClick={() => setOpen(true)}>打开搜索</button><ResearchSearchDialog open={open} identity={identity} onOpenChange={setOpen} onSelect={vi.fn()} /></>
    }
    render(<Preview />)
    const user = userEvent.setup()
    const trigger = screen.getByRole('button', { name: '打开搜索' })
    await user.click(trigger)
    expect(document.activeElement).toBe(screen.getByRole('combobox'))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })
  it('空查询不请求，展示命中片段和高亮，点击携带消息定位信息', async () => {
    const select = vi.fn()
    const close = vi.fn()
    render(<ResearchSearchDialog open identity={identity} onOpenChange={close} onSelect={select} />)
    expect(search).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '宁德时代' } })
    const option = await screen.findByRole('option')
    expect(option.textContent).toContain('历史研究')
    expect(option.querySelector('mark')?.textContent).toBe('宁德时代')
    await userEvent.click(option)
    expect(select).toHaveBeenCalledWith(hit)
    expect(close).toHaveBeenCalledWith(false)
  })

  it('迟到的搜索不覆盖新词，支持键盘选择和加载更多', async () => {
    let finishOld!: (value: unknown) => void
    search.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
      .mockResolvedValueOnce({ items: [hit], next_offset: 1 })
      .mockResolvedValueOnce({ items: [{ ...hit, message_id: 'a2', snippet: '宁德时代另一条回答' }], next_offset: null })
    const select = vi.fn()
    render(<ResearchSearchDialog open identity={identity} onOpenChange={vi.fn()} onSelect={select} />)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '旧词' } })
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '宁德时代' } })
    await screen.findByRole('option')
    finishOld({ items: [{ ...hit, title: '旧结果' }], next_offset: null })
    await userEvent.click(screen.getByRole('button', { name: '加载更多' }))
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2))
    expect(screen.queryByText('旧结果')).toBeNull()
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    expect(select.mock.calls[0][0].message_id).toBe('a2')
  })

  it('搜索失败提供重试，空结果不会显示旧命中', async () => {
    search.mockRejectedValueOnce(new Error('搜索暂时不可用，请重试。')).mockResolvedValueOnce({ items: [], next_offset: null })
    render(<ResearchSearchDialog open identity={identity} onOpenChange={vi.fn()} onSelect={vi.fn()} />)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '不存在' } })
    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByText('没有找到相关内容')
    expect(screen.queryByRole('option')).toBeNull()
  })
})
