# InvestWise AI 研究前端实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 `frontend/` 原生 TypeScript 调试界面替换为 InvestWise 的 React AI 研究工作台，保留登录、会话、SSE、图片上传和证据能力，不展示内部 Trace。

**Architecture:** 使用 React 组件化重建应用外壳和 AI 研究工作台，保留现有 `auth.ts`、`agent-stream.ts`、`conversation-session.ts` 作为稳定协议适配层。AI Elements 以 shadcn 注册表源码组件形式接入 Vite，不使用 Next.js 路由和 AI SDK 默认聊天协议；页面仅实现 AI 研究，其他主菜单显示为未开放导航项。

**Tech Stack:** React 19、TypeScript、Vite、Tailwind CSS 4、shadcn/ui、AI Elements、Lucide/Iconfont 线性图标风格、Vitest、Testing Library。

---

## 文件结构

```text
frontend/src/
├── app/
│   ├── app.tsx                     # 应用启动、身份和研究状态编排
│   └── app-shell.tsx               # InvestWise 顶部栏和主菜单
├── components/
│   ├── ai-elements/                # AI Elements 注册表源码组件
│   └── ui/                         # shadcn 基础组件
├── features/research/
│   ├── research-workspace.tsx      # 左侧历史 + 中央对话主布局
│   ├── research-sidebar.tsx       # 新建研究、当前/本地历史
│   ├── research-thread.tsx        # AI Elements 消息、进度、推理概览
│   ├── research-composer.tsx      # 输入、附件和停止/发送
│   ├── evidence-citation.tsx      # 内联数字证据索引
│   ├── evidence-drawer.tsx        # 当前页右侧证据抽屉
│   ├── research-api.ts            # 旧后端 REST/SSE 适配
│   ├── research-model.ts          # UI 类型与证据索引映射
│   ├── hooks/
│   │   ├── use-research-session.ts      # 会话恢复、流式发送和取消
│   │   ├── use-research-history.ts      # 本地研究列表与激活会话
│   │   ├── use-research-attachments.ts  # 附件上传、轮询、移除和错误恢复
│   │   └── use-evidence-drawer.ts       # 索引选中、定位和焦点恢复
│   └── *.test.tsx                 # 行为测试
├── lib/utils.ts                        # `cn` 等无状态工具
├── main.tsx
└── styles.css                          # Tailwind 主题 token 和极少全局样式
```

## 可维护性约束

- 页面组件不直接调用 `fetch`、`localStorage` 或解析 SSE，这些能力分别收敛到 API adapter 和业务 Hook。
- Hook 按业务用例拆分，而不是每个 `useState` 都抽一层；只有具备独立输入/输出、业务规则或复用价值的逻辑才进入 Hook。
- `useResearchSession` 仅编排会话与流式回答；附件生命周期交给 `useResearchAttachments`，历史会话交给 `useResearchHistory`，证据抽屉交给 `useEvidenceDrawer`。
- Hook 返回 `sendQuestion`、`startNewResearch`、`openEvidence` 等语义化命令，不向组件暴露大量原始 setter。
- API DTO、业务模型与组件 Props 保持分层；后端字段变化只在 `research-api.ts` 和映射函数中消化。
- 每个业务 Hook 有独立行为测试，页面测试不重复验证 Hook 内部实现。

### Task 1: React、Tailwind 和测试基线

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/vite.config.ts`
- Modify: `frontend/tsconfig.json`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/main.tsx`
- Modify: `frontend/index.html`

- [ ] **Step 1: 写入失败的 React 启动测试**

```tsx
import { render, screen } from '@testing-library/react'
import { App } from './app/app'

it('renders the InvestWise AI research application', () => {
  render(<App />)
  expect(screen.getByRole('heading', { name: 'AI 研究' })).toBeInTheDocument()
})
```

- [ ] **Step 2: 运行测试并确认因 React 入口尚未存在而失败**

Run: `cd frontend && npm test -- src/app/app.test.tsx`  
Expected: FAIL，缺少 `app/app` 或 React 测试环境。

- [ ] **Step 3: 升级依赖并建立 Vite React 入口**

安装 React 19、`@vitejs/plugin-react`、Tailwind CSS 4、`@tailwindcss/vite`、Testing Library、jsdom、`lucide-react`、`class-variance-authority`、`clsx`、`tailwind-merge`和 AI Elements 所需的 Radix 依赖。`main.tsx` 使用 `createRoot` 渲染 `<App />`，Vitest 配置 `jsdom` 与 `setupFiles`。

- [ ] **Step 4: 运行启动测试和构建**

Run: `cd frontend && npm test -- src/app/app.test.tsx && npm run build`  
Expected: PASS，Vite 产生 `dist/`。

- [ ] **Step 5: 提交基线迁移**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/tsconfig.json frontend/index.html frontend/src/main.tsx frontend/src/test/setup.ts frontend/src/app/app.test.tsx
git commit -m "build: migrate frontend baseline to React"
```

### Task 2: 研究模型与证据索引

**Files:**
- Create: `frontend/src/features/research/research-model.ts`
- Create: `frontend/src/features/research/research-model.test.ts`
- Preserve: `frontend/src/agent-stream.ts`
- Preserve: `frontend/src/conversation-session.ts`

- [ ] **Step 1: 写入证据稳定编号失败测试**

```ts
it('assigns a stable list index to repeated evidence ids', () => {
  const result = indexEvidence(['ev-b', 'ev-a', 'ev-b'])
  expect(result.citations).toEqual([1, 2, 1])
  expect(result.items.map((item) => item.evidenceId)).toEqual(['ev-b', 'ev-a'])
})
```

- [ ] **Step 2: 运行并确认缺少 `indexEvidence` 而失败**

Run: `cd frontend && npm test -- src/features/research/research-model.test.ts`  
Expected: FAIL with missing export.

- [ ] **Step 3: 实现 UI 模型和索引规则**

```ts
export type EvidenceItem = {
  evidenceId: string
  index: number
  sourceName: string
  publishedAt?: string
  title: string
  excerpt: string
  sourceUrl?: string
}

export function indexEvidence(ids: string[]) {
  const unique: string[] = []
  const indexById = new Map<string, number>()
  const citations = ids.map((id) => {
    if (!indexById.has(id)) {
      unique.push(id)
      indexById.set(id, unique.length)
    }
    return indexById.get(id)!
  })
  return {
    citations,
    items: unique.map((evidenceId, offset) => ({ evidenceId, index: offset + 1 }))
  }
}
```

- [ ] **Step 4: 运行模型测试与现有 SSE/会话测试**

Run: `cd frontend && npm test -- src/features/research/research-model.test.ts src/agent-stream.test.ts src/conversation-session.test.ts`  
Expected: PASS.

- [ ] **Step 5: 提交模型**

```bash
git add frontend/src/features/research/research-model.ts frontend/src/features/research/research-model.test.ts
git commit -m "feat: define AI research presentation model"
```

### Task 3: InvestWise 应用外壳与 AI 研究布局

**Files:**
- Create: `frontend/src/app/app.tsx`
- Create: `frontend/src/app/app-shell.tsx`
- Create: `frontend/src/features/research/research-workspace.tsx`
- Create: `frontend/src/features/research/research-sidebar.tsx`
- Create: `frontend/src/app/app-shell.test.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: 写入导航和默认工作台失败测试**

```tsx
it('opens AI research and leaves unreleased menus disabled', () => {
  render(<App />)
  expect(screen.getByRole('link', { name: 'AI 研究' })).toHaveAttribute('aria-current', 'page')
  expect(screen.getByRole('button', { name: '市场' })).toHaveAttribute('aria-disabled', 'true')
  expect(screen.getByRole('button', { name: '新建研究' })).toBeEnabled()
})
```

- [ ] **Step 2: 运行并确认布局组件缺失**

Run: `cd frontend && npm test -- src/app/app-shell.test.tsx`  
Expected: FAIL.

- [ ] **Step 3: 实现应用外壳**

使用机构级浅色金融终端风格：深海军蓝导航、白色主内容、低饱和金作 AI 点缀。主菜单显示市场、自选、资讯、持仓、策略与提醒、AI 研究、我的；只有 AI 研究可用。桌面端左侧研究列表宽 280px，中央对话区占满剩余空间。

- [ ] **Step 4: 运行布局测试**

Run: `cd frontend && npm test -- src/app/app-shell.test.tsx`  
Expected: PASS.

- [ ] **Step 5: 提交外壳**

```bash
git add frontend/src/app frontend/src/features/research/research-workspace.tsx frontend/src/features/research/research-sidebar.tsx frontend/src/styles.css
git commit -m "feat: add InvestWise AI research shell"
```

### Task 4: AI Elements 对话、进度和输入器

**Files:**
- Create: `frontend/src/components/ai-elements/conversation.tsx`
- Create: `frontend/src/components/ai-elements/message.tsx`
- Create: `frontend/src/components/ai-elements/prompt-input.tsx`
- Create: `frontend/src/components/ai-elements/reasoning.tsx`
- Create: `frontend/src/components/ui/*.tsx`
- Create: `frontend/src/features/research/research-thread.tsx`
- Create: `frontend/src/features/research/research-composer.tsx`
- Create: `frontend/src/features/research/research-thread.test.tsx`

- [ ] **Step 1: 写入流式回答和不导出失败测试**

```tsx
it('renders research progress and does not expose trace or export', () => {
  render(<ResearchThread messages={messages} progress={{ stage: 'retrieval', message: '正在检索公告' }} />)
  expect(screen.getByText('正在检索公告')).toBeVisible()
  expect(screen.queryByText(/trace/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /导出/ })).not.toBeInTheDocument()
})
```

- [ ] **Step 2: 运行并确认对话组件缺失**

Run: `cd frontend && npm test -- src/features/research/research-thread.test.tsx`  
Expected: FAIL.

- [ ] **Step 3: 安装并适配 AI Elements 子集**

从 AI Elements 注册表引入 `Conversation`、`Message`、`PromptInput`、`Reasoning`，保留其无障碍和自动滚动行为。不引入 `ConversationDownload`。`ResearchThread` 将现有 thinking section 映射为可折叠“分析进度”，不展示原始节点 Trace。

- [ ] **Step 4: 运行对话测试**

Run: `cd frontend && npm test -- src/features/research/research-thread.test.tsx`  
Expected: PASS.

- [ ] **Step 5: 提交 AI 对话 UI**

```bash
git add frontend/src/components frontend/src/features/research/research-thread.tsx frontend/src/features/research/research-composer.tsx frontend/src/features/research/research-thread.test.tsx
git commit -m "feat: build AI Elements research conversation"
```

### Task 5: 证据索引与右侧抽屉

**Files:**
- Create: `frontend/src/features/research/evidence-citation.tsx`
- Create: `frontend/src/features/research/evidence-drawer.tsx`
- Create: `frontend/src/features/research/hooks/use-evidence-drawer.ts`
- Create: `frontend/src/features/research/hooks/use-evidence-drawer.test.tsx`
- Create: `frontend/src/features/research/evidence-drawer.test.tsx`

- [ ] **Step 1: 写入证据抽屉失败测试**

```tsx
it('opens the current-page drawer at the cited evidence index', async () => {
  const user = userEvent.setup()
  render(<EvidenceHarness selectedIndex={15} />)
  await user.click(screen.getByRole('button', { name: '查看证据列表第 15 条' }))
  expect(screen.getByRole('dialog', { name: '证据来源' })).toBeVisible()
  expect(screen.getByText('第 15 条证据')).toHaveAttribute('data-active', 'true')
})
```

- [ ] **Step 2: 运行并确认证据组件缺失**

Run: `cd frontend && npm test -- src/features/research/evidence-drawer.test.tsx`  
Expected: FAIL.

- [ ] **Step 3: 实现索引和抽屉**

数字圆点表示当前完整证据列表中的序号。`useEvidenceDrawer` 管理打开、关闭、选中索引、滚动定位和触发按钮焦点恢复；Drawer 组件只负责渲染。只有存在 `sourceUrl` 时才显示“打开资料”，使用 `target="_blank" rel="noopener noreferrer"`。

- [ ] **Step 4: 运行抽屉测试**

Run: `cd frontend && npm test -- src/features/research/evidence-drawer.test.tsx`  
Expected: PASS.

- [ ] **Step 5: 提交证据 UI**

```bash
git add frontend/src/features/research/evidence-citation.tsx frontend/src/features/research/evidence-drawer.tsx frontend/src/features/research/evidence-drawer.test.tsx
git commit -m "feat: add indexed evidence drawer"
```

### Task 6: 现有后端、会话和图片适配

**Files:**
- Create: `frontend/src/features/research/research-api.ts`
- Create: `frontend/src/features/research/hooks/use-research-session.ts`
- Create: `frontend/src/features/research/hooks/use-research-session.test.tsx`
- Create: `frontend/src/features/research/hooks/use-research-history.ts`
- Create: `frontend/src/features/research/hooks/use-research-history.test.tsx`
- Create: `frontend/src/features/research/hooks/use-research-attachments.ts`
- Create: `frontend/src/features/research/hooks/use-research-attachments.test.tsx`
- Modify: `frontend/src/app/app.tsx`
- Preserve: `frontend/src/auth.ts`
- Preserve: `frontend/src/service-status.ts`

- [ ] **Step 1: 写入 SSE 集成失败测试**

```tsx
it('appends answer deltas and clears progress when answer starts', async () => {
  const stream = fakeSseStream([
    ['progress', { stage: 'retrieval', message: '正在检索' }],
    ['answer_delta', { content: '收入保持增长' }]
  ])
  const result = await consumeResearchStream(stream)
  expect(result.answer).toBe('收入保持增长')
  expect(result.progress).toBeNull()
})
```

- [ ] **Step 2: 运行并确认适配器缺失**

Run: `cd frontend && npm test -- src/features/research/hooks/use-research-session.test.tsx`  
Expected: FAIL.

- [ ] **Step 3: 保留原有协议并替换 UI 编排**

`research-api.ts` 封装现有会话消息、SSE 和 Asset API。`useResearchSession` 管理恢复、发送、取消和错误恢复；`useResearchHistory` 管理当前用户的本地研究索引；`useResearchAttachments` 管理图片上传、分析轮询、移除和资源释放。`agent_event` 只用于内部状态，不渲染 Trace 面板。

- [ ] **Step 4: 运行集成和回归测试**

Run: `cd frontend && npm test`  
Expected: 全部测试 PASS。

- [ ] **Step 5: 提交数据适配**

```bash
git add frontend/src/features/research/research-api.ts frontend/src/features/research/hooks frontend/src/app/app.tsx
git commit -m "feat: connect InvestWise research to agent backend"
```

### Task 7: 响应式、可访问性与最终验证

**Files:**
- Modify: `frontend/src/styles.css`
- Modify: `frontend/README.md`
- Delete: `frontend/src/main.ts`
- Test: `frontend/src/**/*.test.ts?(x)`

- [ ] **Step 1: 增加手机端和键盘行为测试**

```tsx
it('keeps citation controls accessible by keyboard', async () => {
  const user = userEvent.setup()
  render(<EvidenceCitation index={2} onOpen={() => undefined} />)
  await user.tab()
  expect(screen.getByRole('button', { name: '查看证据列表第 2 条' })).toHaveFocus()
})
```

- [ ] **Step 2: 完成响应式和减少动效样式**

1024px 以上显示完整左栏；768–1023px 收窄主导航；手机端将历史收入 Drawer，证据 Drawer 接近全屏。交互控件最小命中区 44px，保留 `focus-visible`，遵守 `prefers-reduced-motion`。

- [ ] **Step 3: 删除旧的 DOM 入口并更新前端说明**

删除 `main.ts` 中用户可见 Trace、Artifact、Tenant 和 Session 调试界面。README 记录 React/Vite/Tailwind/AI Elements 架构、运行命令及后端协议仍为自定义 SSE。

- [ ] **Step 4: 运行完整验证**

Run: `cd frontend && npm test && npm run build`  
Expected: 测试 0 失败，TypeScript 编译和 Vite 构建成功。

- [ ] **Step 5: 运行桌面与手机可见 QA**

Run: `cd frontend && npm run dev`  
Expected: 在 1440px、1024px 和 390px 视口下，工作台无横向溢出；发送、滚动、证据抽屉和外链可操作；界面无 Trace 和导出操作。

- [ ] **Step 6: 提交完整前端**

```bash
git add frontend
git commit -m "feat: deliver InvestWise AI research workspace"
```
