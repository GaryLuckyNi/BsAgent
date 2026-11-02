// @vitest-environment jsdom

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ResearchHeader } from './research-header'

describe('ResearchHeader', () => {
  it('展示当前会话真实摘要', () => {
    render(<ResearchHeader title="分析排产" preview="最新公告显示排产回升" />)
    expect(screen.getByText('最新公告显示排产回升')).toBeTruthy()
    expect(screen.queryByText('整合市场、公告与宏观信息，形成可追溯的投资观点。')).toBeNull()
  })
  it('只显示研究标题，不显示连接或演示状态', () => {
    render(<ResearchHeader title="A 股三季度风格展望" />)

    expect(screen.getByRole('heading', { name: 'A 股三季度风格展望' })).toBeTruthy()
    expect(screen.queryByText('正在连接')).toBeNull()
    expect(screen.queryByText('研究服务在线')).toBeNull()
    expect(screen.queryByText('演示模式')).toBeNull()
  })
})
