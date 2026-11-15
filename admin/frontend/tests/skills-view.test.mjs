// @vitest-environment jsdom
import { createApp, nextTick } from 'vue'
import ElementPlus from 'element-plus'
import SkillsView from '../src/views/SkillsView.vue'

const api = vi.hoisted(() => ({ update: vi.fn(async () => ({})) }))
vi.mock('../src/api/skills', () => ({
  listSkills: async () => ({ items: [{
    name: 'company_snapshot', version: '1', summary: '很长的流程说明。'.repeat(80),
    supported_intents: ['task_workflow'], supported_domains: ['a_share_equity_research'],
    supported_sub_intents: ['company_snapshot'], risk_level: 'medium', enabled: true
  }] }),
  updateSkill: api.update, createSkill: vi.fn(), deleteSkill: vi.fn()
}))
vi.mock('../src/api/businessIntents', () => ({
  listBusinessIntents: async () => ({ items: [{
    id: 'general', intent_type: 'financial_research', domain: 'a_share_equity_research',
    subdomain: 'general_research', enabled: true
  }] })
}))

let app
afterEach(() => { app?.unmount(); document.body.replaceChildren(); api.update.mockClear() })
async function settle() { await new Promise((resolve) => setTimeout(resolve, 0)); await nextTick() }
async function mount() {
  const root = document.createElement('div')
  document.body.append(root)
  app = createApp(SkillsView).use(ElementPlus)
  app.mount(root)
  await settle()
}

it('失效分类标签可以移除并保存，不强迫保留历史值', async () => {
  await mount()
  const edit = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === '编辑')
  edit.click()
  await settle()
  const dialog = document.querySelector('.el-dialog')
  const oldIntent = dialog.querySelectorAll('.el-select')[0]
  const remove = oldIntent.querySelector('.el-tag__close')
  expect(remove, '失效选项被 disabled 后没有移除按钮').not.toBeNull()
  remove.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await settle()
  expect(oldIntent.textContent).not.toContain('task_workflow')
  const save = [...dialog.querySelectorAll('button')].find((b) => b.textContent.trim() === '保存')
  save.click()
  await settle()
  expect(api.update).toHaveBeenCalledWith('company_snapshot', expect.objectContaining({
    supported_intents: [], supported_domains: [], supported_sub_intents: []
  }))
})

it('Summary 的真实悬浮弹层使用专属限宽样式类', async () => {
  await mount()
  // jsdom 不做文字排版；仅补充浏览器几何边界，不替换 Element Plus 的提示逻辑。
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true, value: () => ({ width: 2000, height: 20, top: 0, left: 0, right: 2000, bottom: 20 })
  })
  const cell = document.querySelector('.el-table__body tr td:nth-child(3)')
  Object.defineProperty(cell.querySelector('.cell'), 'scrollWidth', { value: 2000 })
  cell.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }))
  await settle()
  expect(document.querySelector('.skill-summary-tooltip'), 'Column 的 tooltip-options 不生效').not.toBeNull()
  expect(document.querySelector('.skill-summary-tooltip').textContent).toContain('完整流程请点击“编辑”查看')
})
