// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ResearchSidebar } from './research-sidebar'

// JSDOM 不实现浏览器原生 :modal/:fullscreen 状态；当前 nwsapi 会递归调用
// Element.matches。只补齐这两个恒为 false 的状态，保留真实的 Radix 交互。
beforeEach(() => {
  const matches = Element.prototype.matches
  vi.spyOn(Element.prototype, 'matches').mockImplementation(function (this: Element, selector) {
    if (selector === ':modal' || selector === ':fullscreen') return false
    return matches.call(this, selector)
  })
})

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('ResearchSidebar', () => {
  it('收起保留新建和搜索入口，展开后保留列表节点与滚动位置', async () => {
    const create = vi.fn()
    const search = vi.fn()
    function Preview() {
      const [collapsed, setCollapsed] = useState(false)
      return <ResearchSidebar threads={[{ id: 's1', title: '保留的会话', preview: '摘要', updatedAt: '' }]} activeThreadId="s1" onActivate={vi.fn()} onCreate={create} onSearch={search} onDelete={vi.fn()} collapsed={collapsed} onToggleCollapsed={() => setCollapsed(!collapsed)} />
    }
    render(<Preview />)
    const list = screen.getByRole('navigation', { name: '研究历史' })
    list.scrollTop = 120
    await userEvent.click(screen.getByRole('button', { name: '收起侧栏' }))
    expect(screen.queryByRole('navigation', { name: '研究历史' })).toBeNull()
    const expand = screen.getByRole('button', { name: '展开侧栏' })
    expect(document.activeElement).toBe(expand)
    expect(expand.getAttribute('aria-expanded')).toBe('false')
    await userEvent.click(screen.getByRole('button', { name: '新建研究' }))
    await userEvent.click(screen.getByRole('button', { name: '搜索研究记录' }))
    expect(create).toHaveBeenCalledTimes(1)
    expect(search).toHaveBeenCalledTimes(1)
    await userEvent.click(expand)
    expect(screen.getByRole('navigation', { name: '研究历史' })).toBe(list)
    expect(list.scrollTop).toBe(120)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '收起侧栏' }))
  })
  it('滚动时显示滚动条，连续滚动延长显示，停止后隐藏并清理计时器', () => {
    vi.useFakeTimers()
    const { unmount } = render(<ResearchSidebar threads={[]} activeThreadId="" onActivate={vi.fn()} onCreate={vi.fn()} onSearch={vi.fn()} onDelete={vi.fn()} />)
    const list = screen.getByRole('navigation', { name: '研究历史' })
    expect(list.classList.contains('thread-list--scrolling')).toBe(false)
    fireEvent.scroll(list)
    expect(list.classList.contains('thread-list--scrolling')).toBe(true)
    act(() => vi.advanceTimersByTime(600))
    fireEvent.scroll(list)
    act(() => vi.advanceTimersByTime(600))
    expect(list.classList.contains('thread-list--scrolling')).toBe(true)
    act(() => vi.advanceTimersByTime(200))
    expect(list.classList.contains('thread-list--scrolling')).toBe(false)
    fireEvent.scroll(list)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('保留研究入口且不显示行情待接入卡片', () => {
    render(
      <ResearchSidebar
        threads={[]}
        activeThreadId=""
        onActivate={vi.fn()}
        onCreate={vi.fn()}
        onSearch={vi.fn()}
        onDelete={vi.fn()}
      />
    )

    expect(screen.getByRole('button', { name: /新建研究/ })).toBeTruthy()
    expect(screen.queryByText('行情数据待接入')).toBeNull()
  })

  it('操作菜单先确认，取消不删除，确认才调用删除接口', async () => {
    const onDelete = vi.fn(async () => {})
    const onActivate = vi.fn()
    const onSearch = vi.fn()
    render(<ResearchSidebar threads={[{ id: 's1', title: '实际会话', preview: '回答', updatedAt: '2026-10-28T01:00:00Z' }]}
      activeThreadId="s1" onActivate={onActivate} onCreate={vi.fn()}
      onSearch={onSearch} onDelete={onDelete} />)
    fireEvent.click(screen.getByRole('button', { name: '搜索研究记录' }))
    expect(onSearch).toHaveBeenCalledTimes(1)
    const user = userEvent.setup()
    const openDelete = async () => {
      await user.click(screen.getByRole('button', { name: '更多操作' }))
      await user.click(await screen.findByRole('menuitem', { name: '删除' }))
      await screen.findByText('删除这条研究？')
    }
    await openDelete()
    expect(onActivate).not.toHaveBeenCalled()
    expect(onDelete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('取消'))
    expect(onDelete).not.toHaveBeenCalled()
    await openDelete()
    fireEvent.click(screen.getByText('删除研究'))
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith('s1'))
  })

  it('删除中禁止重复提交和关闭，失败后保留确认框并支持重试', async () => {
    let rejectDelete!: (error: Error) => void
    const onDelete = vi.fn()
      .mockImplementationOnce(() => new Promise<void>((_resolve, reject) => { rejectDelete = reject }))
      .mockResolvedValue(undefined)
    render(<ResearchSidebar threads={[{ id: 's1', title: '实际会话', preview: '回答', updatedAt: '2026-10-28T01:00:00Z' }]}
      activeThreadId="s1" onActivate={vi.fn()} onCreate={vi.fn()}
      onSearch={vi.fn()} onDelete={onDelete} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '更多操作' }))
    await user.click(await screen.findByRole('menuitem', { name: '删除' }))
    await screen.findByRole('dialog', { name: '删除这条研究？' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '取消' }))
    await user.click(screen.getByRole('button', { name: '删除研究' }))
    expect(screen.getByRole('button', { name: '正在删除…' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: '取消' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: '关闭删除确认框' }).hasAttribute('disabled')).toBe(true)
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: '正在删除…' }))
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(onDelete).toHaveBeenCalledTimes(1)
    await act(async () => rejectDelete(new Error('删除失败，请重试。')))
    expect(screen.getByRole('alert').textContent).toBe('删除失败，请重试。')
    expect(screen.getByRole('dialog')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '删除研究' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(onDelete).toHaveBeenCalledTimes(2)
  })

  it('Escape 取消不删除并将焦点还给当前记录的操作按钮', async () => {
    const onDelete = vi.fn()
    render(<ResearchSidebar threads={[{ id: 's1', title: '实际会话', preview: '回答', updatedAt: '2026-10-28T01:00:00Z' }]}
      activeThreadId="s1" onActivate={vi.fn()} onCreate={vi.fn()}
      onSearch={vi.fn()} onDelete={onDelete} />)
    const user = userEvent.setup()
    const more = screen.getByRole('button', { name: '更多操作' })
    await user.click(more)
    await user.click(await screen.findByRole('menuitem', { name: '删除' }))
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(more))
    expect(onDelete).not.toHaveBeenCalled()
  })
})
