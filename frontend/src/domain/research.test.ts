import { describe, expect, it } from 'vitest'

import { buildEvidenceIndex, resolveCitationNumber } from './research'

describe('research evidence model', () => {
  it('keeps evidence order stable and resolves citation numbers from one', () => {
    const evidence = buildEvidenceIndex([
      { id: 'eastmoney-1', title: '东方财富公司公告', url: 'https://example.com/1' },
      { id: 'sse-2', title: '上交所披露', url: 'https://example.com/2' }
    ])

    expect(evidence.map((item) => item.index)).toEqual([1, 2])
    expect(resolveCitationNumber(evidence, 'sse-2')).toBe(2)
    expect(resolveCitationNumber(evidence, 'missing')).toBeUndefined()
  })
})
