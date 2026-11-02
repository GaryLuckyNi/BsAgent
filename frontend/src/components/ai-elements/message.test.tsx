// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { toConversationMessages } from '@/conversation-session'

import { MessageResponse } from './message'

afterEach(cleanup)

it('保存的回答重载后，数字引用仍渲染为可打开原文的链接', () => {
  const [restored] = toConversationMessages([{
    role: 'assistant', status: 'completed', sequence_no: 1, metadata: {},
    content: '行情有据（[1](<https://example.com/market?x=(a)&y=1>)）。'
  }])
  render(<MessageResponse mode="static" isAnimating={false}>
    {restored.content}
  </MessageResponse>)
  const link = screen.getByRole('link', { name: '1' })
  expect(link.getAttribute('href')).toBe('https://example.com/market?x=(a)&y=1')
  expect(link.getAttribute('target')).toBe('_blank')
  expect(link.getAttribute('rel')).toContain('noreferrer')
})
