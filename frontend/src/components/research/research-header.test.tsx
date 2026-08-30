// @vitest-environment jsdom

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ResearchHeader } from './research-header'

describe('ResearchHeader', () => {
  it('只显示研究标题，不显示连接或演示状态', () => {
    render(<ResearchHeader title="A 股三季度风格展望" />)

    expect(screen.getByRole('heading', { name: 'A 股三季度风格展望' })).toBeTruthy()
    expect(screen.queryByText('正在连接')).toBeNull()
    expect(screen.queryByText('研究服务在线')).toBeNull()
    expect(screen.queryByText('演示模式')).toBeNull()
  })
})
