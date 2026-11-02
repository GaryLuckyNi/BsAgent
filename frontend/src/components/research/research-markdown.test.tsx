// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { ResearchMarkdown } from './research-markdown'

beforeEach(() => {
  // JSDOM 的选择器引擎会递归检查未实现的顶层伪类；本测试页面不存在这些原生状态。
  const matches = Element.prototype.matches
  vi.spyOn(Element.prototype, 'matches').mockImplementation(function (this: Element, selector) {
    if ([':modal', ':fullscreen', ':popover-open'].includes(selector)) return false
    return matches.call(this, selector)
  })
})
afterEach(async () => { await act(async () => { cleanup() }); vi.restoreAllMocks() })
const source = { id: 'source_7', index: 7, url: 'https://example.com/report', title: '年度报告', summary: '报告摘要', publishedAt: '2025-04-29' }

it('正文不变时新到的来源元数据仍更新预览，不重编号', () => {
  const { rerender } = render(<ResearchMarkdown mode="static" isAnimating={false} sources={[]}>依据 [7](https://example.com/report)。</ResearchMarkdown>)
  const link = screen.getByRole('link', { name: '引用来源 7' })
  expect(link.getAttribute('class')).toContain('research-citation')
  rerender(<ResearchMarkdown mode="static" isAnimating={false} sources={[source]}>依据 [7](https://example.com/report)。</ResearchMarkdown>)
  fireEvent.focus(link)
  expect(screen.getByText('年度报告')).toBeTruthy()
  expect(screen.getByText('报告摘要')).toBeTruthy()
  const icon = document.querySelector('.citation-source-icon img')!
  expect(icon.getAttribute('src')).toBe('https://example.com/favicon.ico')
  fireEvent.error(icon)
  expect(document.querySelector('.citation-source-icon img')).toBeNull()
})

it('普通文字链接不变，缺少元数据的旧编号只展示地址和域名', () => {
  render(<ResearchMarkdown mode="static" isAnimating={false}>📜 **政策风险**：[官方网站](https://example.com) [4](https://other.example/a)</ResearchMarkdown>)
  expect(screen.getByRole('link', { name: '官方网站' }).className).not.toContain('research-citation')
  fireEvent.focus(screen.getByRole('link', { name: '引用来源 4' }))
  expect(screen.getByText('other.example')).toBeTruthy()
  expect(screen.getByText('暂无摘要，可打开原文查看。')).toBeTruthy()
  expect(screen.queryByText('年度报告')).toBeNull()
  expect(screen.getByText(/政策风险/).closest('strong, [data-streamdown="strong"]')).not.toBeNull()
})

it('来源编号或地址不匹配时，不把别处的资料展示为该引用', () => {
  render(<ResearchMarkdown mode="static" isAnimating={false} sources={[source]}>[7](https://different.example/a)</ResearchMarkdown>)
  fireEvent.focus(screen.getByRole('link', { name: '引用来源 7' }))
  expect(screen.getByText('different.example')).toBeTruthy()
  expect(screen.queryByText('年度报告')).toBeNull()
})

it('在卡片中按 Escape 后关闭预览并将焦点返回引用', () => {
  render(<ResearchMarkdown mode="static" isAnimating={false} sources={[source]}>[7](https://example.com/report)</ResearchMarkdown>)
  const link = screen.getByRole('link', { name: '引用来源 7' })
  fireEvent.focus(link)
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  expect(document.activeElement).toBe(link)
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('悬停后打开预览，指针移入卡片可继续阅读，离开后关闭', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  try {
    render(<ResearchMarkdown mode="static" isAnimating={false} sources={[source]}>[7](https://example.com/report)</ResearchMarkdown>)
    fireEvent.pointerEnter(screen.getByRole('link', { name: '引用来源 7' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await act(async () => { vi.advanceTimersByTime(200) })
    const card = screen.getByRole('dialog')
    fireEvent.pointerLeave(screen.getByRole('link', { name: '引用来源 7' }))
    fireEvent.pointerEnter(card)
    await act(async () => { vi.advanceTimersByTime(250) })
    expect(screen.getByRole('dialog')).toBe(card)
    fireEvent.pointerLeave(card)
    await act(async () => { vi.advanceTimersByTime(220) })
    expect(screen.queryByRole('dialog')).toBeNull()
    await act(async () => { cleanup(); vi.runOnlyPendingTimers() })
  } finally { vi.useRealTimers() }
})
