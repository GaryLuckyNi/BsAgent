// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useEvidenceDrawer } from './use-evidence-drawer'

describe('useEvidenceDrawer', () => {
  it('opens at the selected evidence index and can close', () => {
    const { result } = renderHook(() => useEvidenceDrawer())

    act(() => result.current.openEvidence(15))
    expect(result.current.isOpen).toBe(true)
    expect(result.current.activeIndex).toBe(15)

    act(() => result.current.closeEvidence())
    expect(result.current.isOpen).toBe(false)
  })
})
