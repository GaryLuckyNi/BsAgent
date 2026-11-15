# InvestWise AI 研究工作区固定布局实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 移除行情待接入卡片，使 InvestWise 顶部导航和完整输入区域固定可见，并让 AI 对话成为页面唯一的纵向滚动区域。

**Architecture:** 使用视口级 Flex 骨架承载顶部导航与路由页面，研究页内部继续使用三行 Grid 管理标题、对话和输入区。业务组件和数据流保持不变，只移除侧栏展示节点并收紧 CSS 的高度、最小高度和溢出边界。

**Tech Stack:** React 19、TypeScript、Vite、Vitest、Testing Library、CSS Grid/Flex、AI Elements

---

## 文件结构

- 修改 `frontend/src/components/research/research-sidebar.tsx`：移除“行情数据待接入”展示卡片。
- 创建 `frontend/src/components/research/research-sidebar.test.tsx`：覆盖侧栏核心入口仍存在且待接入卡片不再渲染。
- 修改 `frontend/src/styles.css`：建立 `100dvh` 页面骨架、唯一滚动区和移动端安全区。

### Task 1：移除行情待接入卡片

**Files:**
- Create: `frontend/src/components/research/research-sidebar.test.tsx`
- Modify: `frontend/src/components/research/research-sidebar.tsx`
- Modify: `frontend/src/styles.css`

- [x] **Step 1: 编写失败的侧栏回归测试**

```tsx
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
```

- [x] **Step 2: 运行测试并确认先失败**

Run: `cd frontend && npx vitest run src/components/research/research-sidebar.test.tsx`

Expected: FAIL，页面仍能找到“行情数据待接入”。

- [x] **Step 3: 删除侧栏卡片和孤立样式**

从 `ResearchSidebar` 返回值中删除：

```tsx
<div className="watch-card">
  <span>研究助手</span>
  <b>行情数据待接入</b>
  <p>配置 Tushare 后，研究结果将融合实时市场数据。</p>
</div>
```

从 `styles.css` 删除 `.watch-card`、`.watch-card span`、`.watch-card b` 和 `.watch-card p` 规则。

- [x] **Step 4: 运行侧栏测试并确认通过**

Run: `cd frontend && npx vitest run src/components/research/research-sidebar.test.tsx`

Expected: 1 test passed。

- [x] **Step 5: 提交卡片移除**

```bash
git add frontend/src/components/research/research-sidebar.tsx frontend/src/components/research/research-sidebar.test.tsx frontend/src/styles.css
git commit -m "feat(frontend): remove pending market data card"
```

### Task 2：建立固定头部、固定输入区和唯一对话滚动区

**Files:**
- Modify: `frontend/src/styles.css`

- [x] **Step 1: 收紧页面根节点和应用骨架的高度边界**

将根节点和应用骨架调整为视口级布局：

```css
html,
body,
#root {
  width: 100%;
  height: 100%;
  overflow: hidden;
}

body {
  margin: 0;
  min-width: 320px;
  background: #f4f6f8;
}

.app-shell {
  height: 100dvh;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.topbar {
  flex: 0 0 64px;
}
```

- [x] **Step 2: 让路由工作区填满剩余空间而不推高页面**

更新工作区和两侧容器：

```css
.workspace {
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
  height: auto;
  overflow: hidden;
}

.desktop-sidebar,
.research-main {
  min-height: 0;
  overflow: hidden;
}

.research-sidebar {
  min-height: 0;
  overflow-y: auto;
}
```

- [x] **Step 3: 保证只有对话区滚动且输入区完整吸底**

保留 `.research-main` 的三行 Grid，并补充输入区和对话区边界：

```css
.research-main {
  grid-template-rows: auto minmax(0, 1fr) auto;
}

.conversation {
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.composer-wrap {
  flex: 0 0 auto;
  padding-bottom: max(20px, env(safe-area-inset-bottom));
  background: #fff;
}
```

同时让非研究路由的错误页继承剩余高度，避免应用根节点锁定滚动后被裁切：

```css
.route-error {
  flex: 1 1 auto;
  min-height: 0;
}
```

在 `@media (max-width: 820px)` 中把顶部导航高度同步为 `56px`，工作区继续填满剩余空间，不再使用 `calc(100vh - 56px)`。

- [x] **Step 4: 运行全部测试和生产构建**

Run: `cd frontend && npm test`

Expected: 8 个测试文件、30 个测试全部通过。

Run: `cd frontend && npm run build`

Expected: TypeScript 和 Vite 构建成功，退出码为 0。

- [x] **Step 5: 提交固定布局**

```bash
git add frontend/src/styles.css
git commit -m "fix(frontend): constrain AI research workspace scrolling"
```

### Task 3：浏览器验收固定布局

**Files:**
- Verify: `frontend/src/styles.css`
- Verify: `frontend/src/components/research/research-sidebar.tsx`

- [x] **Step 1: 启动开发服务器**

Run: `cd frontend && npm run dev -- --port 5175`

Expected: Vite 在 `http://127.0.0.1:5175/` 提供页面。

- [x] **Step 2: 验证桌面端滚动边界**

在 1440 × 900 视口打开 `/ai-research`，记录 `.topbar`、`.conversation` 和 `.composer-wrap` 的边界位置。滚动 `.conversation` 后确认：

- `window.scrollY === 0`；
- 顶部导航的 `top` 不变；
- 输入区域的 `bottom` 不变且不超出视口；
- `.conversation.scrollTop` 发生变化；
- 页面中不存在“行情数据待接入”。

- [x] **Step 3: 验证移动端布局与侧栏交互**

将视口设为 390 × 844，确认：

- `document.documentElement.scrollWidth === document.documentElement.clientWidth`；
- 输入区域底部位于视口和安全区内；
- 点击“研究记录”后侧栏可打开并关闭；
- 对话区仍可独立滚动。

- [x] **Step 4: 检查控制台并完成最终验证**

确认页面控制台无 error 级别日志，然后重新运行：

```bash
cd frontend
npm test
npm run build
git diff --check
```

Expected: 30/30 测试通过，构建成功，`git diff --check` 无输出。
