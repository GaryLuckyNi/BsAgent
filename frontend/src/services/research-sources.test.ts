import { expect, it } from 'vitest'
import { groupResearchSources, parseResearchSources } from './research-sources'
import { toConversationMessages } from '@/conversation-session'
import contract from '@/security/public-url-contract.json'
import { webSourceUrl } from './research-sources'

it.each(contract)('公共来源 URL 契约 $url', ({ url, allowed }) => {
  expect(!!webSourceUrl(url)).toBe(allowed)
})

it('来源转换保留稀疏编号并拒绝非法地址', () => {
  expect(parseResearchSources([
    { source_id: 'source_7', index: 7, url: 'https://example.com/a', title: '报告', snippet: '摘要', published_at: '2025-04-29' },
    { source_id: 'source_8', index: 8, url: 'javascript:alert(1)', title: '错误' }
  ])).toEqual([{ id: 'source_7', index: 7, url: 'https://example.com/a', title: '报告', summary: '摘要', publishedAt: '2025-04-29' }])
})

it('历史消息恢复来源信息，不再生成占位资料', () => {
  const [message] = toConversationMessages([{ role: 'assistant', status: 'completed', content: '[7](https://example.com/a)', sequence_no: 1,
    metadata: { sources: [{ source_id: 'source_7', index: 7, url: 'https://example.com/a', title: '报告' }] } }])
  expect(message.sources?.[0]).toMatchObject({ id: 'source_7', index: 7, title: '报告' })
})

it('按规范 URL 合并来源并保留编号别名，以真实 Markdown 链接判断引用', () => {
  const sources = parseResearchSources([
    { source_id: 'source_7', index: 7, url: 'https://example.com/report', title: '报告' },
    { source_id: 'source_9', index: 9, url: 'https://example.com:443/report#note', snippet: '补充摘要' },
    { source_id: 'source_10', index: 10, url: 'https://other.example/', title: '背景网页' }
  ])
  const result = groupResearchSources(sources, '事实（[9](<https://example.com/report#note>)）。')
  expect(result).toHaveLength(2)
  expect(result[0]).toMatchObject({ indices: [7, 9], cited: true, summary: '补充摘要', title: '报告' })
  expect(result[1].cited).toBe(false)
})

it('代码、图片、普通文字链接和编号错配不能冒充正文引用', () => {
  const sources = [{ id: 'source_7', index: 7, url: 'https://example.com/a', title: '报告' }]
  const text = '`[7](https://example.com/a)`\n\n```md\n[7](https://example.com/a)\n```\n\n![7](https://example.com/a) [报告](https://example.com/a) [7](https://wrong.example/a)'
  expect(groupResearchSources(sources, text)[0].cited).toBe(false)
  expect(groupResearchSources(sources, '[7][report]\n\n[report]: https://example.com/a')[0].cited).toBe(true)
})
