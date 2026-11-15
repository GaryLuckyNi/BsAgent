// @vitest-environment jsdom

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { EvidenceDrawer } from './evidence-drawer'

describe('EvidenceDrawer', () => {
  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: vi.fn()
    })
  })

  it('显示并高亮目标证据，关闭时通知调用方', () => {
    const onOpenChange = vi.fn()

    render(
      <EvidenceDrawer
        open
        activeIndex={2}
        evidence={[
          {
            id: 'pbc-policy',
            index: 2,
            title: '货币政策执行报告',
            url: 'https://www.pbc.gov.cn/',
            publisher: '中国人民银行',
            publishedAt: '2026-08-15',
            summary: '观察流动性、利率与信用环境。'
          }
        ]}
        onOpenChange={onOpenChange}
      />
    )

    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByRole('heading', { name: '证据来源' })).toBeTruthy()
    expect(screen.getByText('中国人民银行').closest('article')?.classList.contains('evidence-card--active')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
