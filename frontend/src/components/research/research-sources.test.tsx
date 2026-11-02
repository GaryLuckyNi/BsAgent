// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { ResearchSources } from './research-sources'

const sources = [
  { id: 'source_7', index: 7, url: 'https://example.com/report', title: '年度报告', summary: '利润与回款', publishedAt: '2025-04-29' },
  { id: 'source_9', index: 9, url: 'https://example.com/report#note', title: '同一份报告' },
  { id: 'source_10', index: 10, url: 'https://other.example/background', title: '行业动态', summary: '供需变化' }
]
beforeEach(() => {
  const matches = Element.prototype.matches
  vi.spyOn(Element.prototype, 'matches').mockImplementation(function (this: Element, selector) {
    if ([':modal', ':fullscreen', ':popover-open'].includes(selector)) return false
    return matches.call(this, selector)
  })
})
afterEach(async () => { await act(async () => { cleanup() }); vi.restoreAllMocks() })

it('入口按去重网页计数，筛选正文引用并保留原始编号', async () => {
  render(<ResearchSources sources={sources} content="事实 [9](https://example.com/report#note)" />)
  const entry = screen.getByRole('button', { name: /研究来源/ })
  expect(entry.textContent).toContain('2 篇网页')
  expect(entry.textContent).toContain('2 个网站')
  fireEvent.click(entry)
  const panel = screen.getByRole('dialog', { name: '研究来源' })
  expect(within(panel).getAllByRole('link')).toHaveLength(2)
  expect(within(panel).getByRole('link', { name: /年度报告/ }).getAttribute('rel')).toContain('noreferrer')
  await userEvent.click(within(panel).getByRole('tab', { name: /正文引用/ }))
  expect(within(panel).getAllByRole('link')).toHaveLength(1)
  expect(within(panel).getByText('7、9')).toBeTruthy()
  fireEvent.keyDown(panel, { key: 'Escape' })
  await act(async () => {})
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect(document.activeElement).toBe(entry))
})

it('可搜索摘要和网站，空结果后能清空恢复，重新打开不保留旧筛选', async () => {
  render(<ResearchSources sources={sources} content="答案" />)
  fireEvent.click(screen.getByRole('button', { name: /研究来源/ }))
  const input = screen.getByRole('searchbox', { name: '搜索本次来源' })
  fireEvent.change(input, { target: { value: '供需' } })
  expect(screen.getAllByRole('link')).toHaveLength(1)
  expect(screen.getByRole('link').textContent).toContain('行业动态')
  fireEvent.change(input, { target: { value: '无匹配' } })
  expect(screen.getByText('没有找到匹配的网页')).toBeTruthy()
  fireEvent.change(input, { target: { value: 'example.com' } })
  expect(screen.getAllByRole('link')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '关闭研究来源' }))
  await act(async () => {})
  fireEvent.click(screen.getByRole('button', { name: /研究来源/ }))
  expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('')
  expect(screen.getAllByRole('link')).toHaveLength(2)
})

it('没有有效网页时不展示入口', () => {
  render(<ResearchSources sources={[]} content="回答" />)
  expect(screen.queryByRole('button')).toBeNull()
})
