// @vitest-environment jsdom

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ResearchSidebar } from './research-sidebar'

describe('ResearchSidebar', () => {
  it('保留研究入口且不显示行情待接入卡片', () => {
    render(
      <ResearchSidebar
        threads={[]}
        activeThreadId=""
        onActivate={vi.fn()}
        onCreate={vi.fn()}
      />
    )

    expect(screen.getByRole('button', { name: /新建研究/ })).toBeTruthy()
    expect(screen.queryByText('行情数据待接入')).toBeNull()
  })
})
