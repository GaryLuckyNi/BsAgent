# AI 研究首屏滚动与状态标签调整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使 AI 研究页首次打开时从示例回答顶部开始显示，并移除页头的连接或演示状态标签。

**Architecture:** 保留 AI Elements 通用 `Conversation` 的默认行为，只在 `ResearchConversation` 业务边界传入 `initial={false}`。`ResearchHeader` 收窄为纯标题展示组件，工作区不再将健康状态传入页头。

**Tech Stack:** React 19、TypeScript、AI Elements、use-stick-to-bottom、Vitest、Testing Library、Vite

---

## 文件结构

- 新建 `frontend/src/components/research/research-conversation.test.tsx`：验证研究会话对 AI Elements 滚动组件的业务配置。
- 新建 `frontend/src/components/research/research-header.test.tsx`：验证标题内容和状态标签移除行为。
- 修改 `frontend/src/components/research/research-conversation.tsx`：关闭研究页的初始自动滚底。
- 修改 `frontend/src/components/research/research-header.tsx`：移除状态标签及无用入参。
- 修改 `frontend/src/components/research/research-workspace.tsx`：停止传递 `isOnline`。

### Task 1: 锁定首屏滚动行为

**Files:**
- Create: `frontend/src/components/research/research-conversation.test.tsx`
- Modify: `frontend/src/components/research/research-conversation.tsx`

- [ ] **Step 1: 编写失败测试**

```tsx
// @vitest-environment jsdom

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ResearchConversation } from './research-conversation'

vi.mock('@/components/ai-elements/conversation', () => ({
  Conversation: ({ children, initial }: { children: React.ReactNode; initial?: boolean | string }) => (
    <div data-testid="conversation" data-initial={String(initial)}>{children}</div>
  ),
  ConversationContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ConversationScrollButton: () => null
}))

vi.mock('./research-message-item', () => ({
  ResearchMessageItem: () => <div>研究回答</div>
}))

describe('ResearchConversation', () => {
  it('首次渲染时不自动滚动到回答底部', () => {
    render(<ResearchConversation messages={[]} onCitation={vi.fn()} />)

    expect(screen.getByTestId('conversation').dataset.initial).toBe('false')
  })
})
```

- [ ] **Step 2: 运行测试并确认因缺少配置而失败**

Run: `cd frontend && npm test -- src/components/research/research-conversation.test.tsx`

Expected: FAIL，实际 `data-initial` 为 `undefined`，期望值为 `false`。

- [ ] **Step 3: 在业务组件关闭初始滚底**

```tsx
return (
  <Conversation className="conversation" initial={false}>
    <ConversationContent className="conversation-content">
      {messages.map((message) => <ResearchMessageItem key={message.id} message={message} onCitation={onCitation} />)}
    </ConversationContent>
    <ConversationScrollButton />
  </Conversation>
)
```

- [ ] **Step 4: 运行定向测试并确认通过**

Run: `cd frontend && npm test -- src/components/research/research-conversation.test.tsx`

Expected: 1 test passed。

### Task 2: 移除页头状态标签

**Files:**
- Create: `frontend/src/components/research/research-header.test.tsx`
- Modify: `frontend/src/components/research/research-header.tsx`
- Modify: `frontend/src/components/research/research-workspace.tsx`

- [ ] **Step 1: 编写失败测试**

```tsx
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
```

- [ ] **Step 2: 运行测试并确认因状态标签仍存在而失败**

Run: `cd frontend && npm test -- src/components/research/research-header.test.tsx`

Expected: FAIL，页面仍渲染“演示模式”。

- [ ] **Step 3: 精简页头并更新调用方**

`frontend/src/components/research/research-header.tsx`:

```tsx
import { Sparkles } from 'lucide-react'

export function ResearchHeader({ title }: { title?: string }) {
  return (
    <header className="research-header">
      <div>
        <span className="research-kicker"><Sparkles size={14} /> AI 研究</span>
        <h1>{title ?? '投资研究'}</h1>
        <p>整合市场、公告与宏观信息，形成可追溯的投资观点。</p>
      </div>
    </header>
  )
}
```

`frontend/src/components/research/research-workspace.tsx`:

```tsx
<ResearchHeader title={history.activeThread?.title} />
```

- [ ] **Step 4: 运行定向测试并确认通过**

Run: `cd frontend && npm test -- src/components/research/research-header.test.tsx`

Expected: 1 test passed。

- [ ] **Step 5: 提交行为修复**

```bash
git add frontend/src/components/research/research-conversation.test.tsx frontend/src/components/research/research-conversation.tsx frontend/src/components/research/research-header.test.tsx frontend/src/components/research/research-header.tsx frontend/src/components/research/research-workspace.tsx
git commit -m "fix(frontend): keep initial research response at top"
```

### Task 3: 全量验证与浏览器验收

**Files:**
- Verify only; no planned source changes.

- [ ] **Step 1: 运行全部前端测试**

Run: `cd frontend && npm test`

Expected: all test files and tests pass with zero failures.

- [ ] **Step 2: 运行生产构建**

Run: `cd frontend && npm run build`

Expected: TypeScript and Vite build complete with exit code 0.

- [ ] **Step 3: 检查补丁格式**

Run: `git diff --check HEAD^ -- frontend/src/components/research`

Expected: no output.

- [ ] **Step 4: 启动页面并完成浏览器验收**

Run: `cd frontend && npm run dev -- --host 127.0.0.1 --port 5177`

Expected:

- 打开 `http://127.0.0.1:5177/ai-research` 后，会话滚动容器的 `scrollTop` 为 `0`。
- 首屏可见“研究摘要”及回答开头。
- 页头不存在“正在连接”、“研究服务在线”或“演示模式”。
- 浏览器控制台无错误。
