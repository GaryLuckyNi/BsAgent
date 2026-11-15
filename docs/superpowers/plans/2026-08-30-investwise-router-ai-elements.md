# InvestWise Router and Official AI Elements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 React Router 拆分 InvestWise 页面架构，并用 `npx ai-elements@latest` 安装的官方组件替换手写 AI Elements 组件。

**Architecture:** `RouterProvider` 管理根路由，`AppLayout` 提供全局产品框架，`AiResearchView` 只组装 hooks 与页面级状态。官方 AI Elements 组件保留在 `components/ai-elements/`，现有 SSE 业务协议通过 `components/research/` 适配层接入，不改变后端契约。

**Tech Stack:** React 19、React Router、TypeScript、Vite 6、Tailwind CSS 4、shadcn/ui、AI Elements CLI、Vitest、Testing Library

---

## 文件结构

### 新建

- `frontend/components.json`：shadcn/ui 与 AI Elements CLI 的 Vite、Tailwind 4 和 `@/` 别名配置。
- `frontend/src/router/index.tsx`：`createBrowserRouter` 路由表。
- `frontend/src/router/router.test.tsx`：根路径重定向、AI 研究页和错误页测试。
- `frontend/src/layouts/app-layout.tsx`：品牌顶栏、主导航、鉴权入口和 `Outlet`。
- `frontend/src/components/navigation/main-navigation.tsx`：主菜单数据与渲染。
- `frontend/src/views/ai-research/index.tsx`：AI 研究页 hooks 组装。
- `frontend/src/views/route-error/index.tsx`：未知路径和路由错误页。
- `frontend/src/components/research/research-workspace.tsx`：研究侧边栏、移动端历史和主工作区布局。
- `frontend/src/components/research/research-header.tsx`：标题与服务状态。
- `frontend/src/components/research/research-conversation.tsx`：官方 `Conversation` 容器与消息序列。
- `frontend/src/components/research/research-message-item.tsx`：官方 `Message`、`MessageResponse` 和 `Reasoning` 适配。
- `frontend/src/components/research/research-composer.tsx`：官方 `PromptInput` 与附件/发送命令适配。
- `frontend/src/utils/cn.ts`：通用 Tailwind class 合并函数。
- `frontend/src/typings/runtime-config.d.ts`：确实属于全局 `Window` 扩展的运行时配置声明。
- `frontend/src/components/ai-elements/*.tsx`：由 AI Elements CLI 官方 registry 生成。
- `frontend/src/components/ui/*.tsx`：由 CLI 按依赖生成的 shadcn/ui 组件。

### 修改

- `frontend/package.json`：加入 React Router 及 CLI 生成组件依赖。
- `frontend/package-lock.json`：锁定依赖。
- `frontend/tsconfig.json`：配置 `baseUrl` 和 `@/*` alias。
- `frontend/vite.config.ts`：配置 `@` 到 `src/` 的 resolve alias。
- `frontend/src/main.tsx`：只渲染 `RouterProvider`。
- `frontend/src/styles.css`：接入 shadcn CSS variables，将业务样式调整为官方 AI Elements 组件的 className 适配。
- `frontend/README.md`：更新路由、目录与 AI Elements 安装来源。

### 删除或迁移

- 删除 `frontend/src/App.tsx`。
- 删除原手写 `frontend/src/components/ai-elements/conversation.tsx`、`message.tsx`、`prompt-input.tsx`、`reasoning.tsx`，随后由 CLI 重新生成官方文件。
- 迁移 `frontend/src/lib/cn.ts` 到 `frontend/src/utils/cn.ts`。
- 迁移 `frontend/src/runtime-config.d.ts` 到 `frontend/src/typings/runtime-config.d.ts`。

### Task 1: 建立 React Router 测试基线

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `frontend/src/router/router.test.tsx`

- [ ] **Step 1: 安装 React Router**

Run:

```bash
cd frontend
npm install react-router-dom
```

Expected: `package.json` 出现 `react-router-dom`，`package-lock.json` 更新。

- [ ] **Step 2: 写入失败的路由测试**

```tsx
// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { routes } from './index'

describe('InvestWise routes', () => {
  it('redirects the root path to AI research', async () => {
    const router = createMemoryRouter(routes, { initialEntries: ['/'] })
    render(<RouterProvider router={router} />)
    expect(await screen.findByRole('heading', { name: /A 股三季度风格展望/ })).toBeTruthy()
    expect(router.state.location.pathname).toBe('/ai-research')
  })

  it('renders a route error for an unknown path', async () => {
    const router = createMemoryRouter(routes, { initialEntries: ['/missing'] })
    render(<RouterProvider router={router} />)
    expect(await screen.findByRole('heading', { name: '页面未找到' })).toBeTruthy()
  })
})
```

- [ ] **Step 3: 运行路由测试并确认失败**

Run: `npm test -- --run src/router/router.test.tsx`

Expected: FAIL，提示 `./index` 不存在。

- [ ] **Step 4: 提交测试基线**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/router/router.test.tsx
git commit -m "test(frontend): define InvestWise route behavior"
```

### Task 2: 配置 Vite、shadcn/ui 与工具目录

**Files:**
- Create: `frontend/components.json`
- Create: `frontend/src/utils/cn.ts`
- Create: `frontend/src/typings/runtime-config.d.ts`
- Modify: `frontend/tsconfig.json`
- Modify: `frontend/vite.config.ts`
- Delete: `frontend/src/lib/cn.ts`
- Delete: `frontend/src/runtime-config.d.ts`
- Modify: imports under `frontend/src/components/research/`

- [ ] **Step 1: 配置 TypeScript alias**

在 `compilerOptions` 中加入：

```json
"baseUrl": ".",
"paths": {
  "@/*": ["./src/*"]
}
```

- [ ] **Step 2: 配置 Vite alias**

```ts
import { fileURLToPath, URL } from 'node:url'

resolve: {
  alias: {
    '@': fileURLToPath(new URL('./src', import.meta.url))
  }
}
```

如 TypeScript 报告 `node:url` 类型缺失，运行 `npm install -D @types/node`。

- [ ] **Step 3: 写入 shadcn 配置**

```json
{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "new-york",
  "rsc": false,
  "tsx": true,
  "tailwind": {
    "config": "",
    "css": "src/styles.css",
    "baseColor": "slate",
    "cssVariables": true,
    "prefix": ""
  },
  "iconLibrary": "lucide",
  "aliases": {
    "components": "@/components",
    "utils": "@/utils/cn",
    "ui": "@/components/ui",
    "lib": "@/utils",
    "hooks": "@/hooks"
  }
}
```

- [ ] **Step 4: 迁移工具与全局声明**

`frontend/src/utils/cn.ts`:

```ts
import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
```

`frontend/src/typings/runtime-config.d.ts`:

```ts
import type { RuntimeConfig } from '@/auth'

declare global {
  interface Window {
    __BS_AGENT_RUNTIME_CONFIG__?: RuntimeConfig
  }
}

export {}
```

将所有 `lib/cn` import 替换为 `@/utils/cn`。

- [ ] **Step 5: 验证基础配置**

Run: `npm run build`

Expected: PASS，不存在 alias 或声明文件错误。

- [ ] **Step 6: 提交配置**

```bash
git add frontend/components.json frontend/tsconfig.json frontend/vite.config.ts frontend/package.json frontend/package-lock.json frontend/src/utils frontend/src/typings frontend/src/components/research
git add -u frontend/src/lib frontend/src/runtime-config.d.ts
git commit -m "chore(frontend): configure aliases for AI Elements"
```

### Task 3: 通过官方 CLI 安装 AI Elements

**Files:**
- Replace: `frontend/src/components/ai-elements/conversation.tsx`
- Replace: `frontend/src/components/ai-elements/message.tsx`
- Replace: `frontend/src/components/ai-elements/prompt-input.tsx`
- Replace: `frontend/src/components/ai-elements/reasoning.tsx`
- Create: `frontend/src/components/ui/*.tsx`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: 记录手写组件的文件指纹**

Run:

```bash
shasum src/components/ai-elements/conversation.tsx \
  src/components/ai-elements/message.tsx \
  src/components/ai-elements/prompt-input.tsx \
  src/components/ai-elements/reasoning.tsx
```

Expected: 输出 4 个手写文件的旧指纹，供 CLI 安装后对比。

- [ ] **Step 2: 用官方 CLI 覆盖安装组件**

Run from `frontend/`:

```bash
npx ai-elements@latest add conversation --overwrite
npx ai-elements@latest add message --overwrite
npx ai-elements@latest add prompt-input --overwrite
npx ai-elements@latest add reasoning --overwrite
```

Expected: CLI 显示 registry 校验成功，并在 `src/components/ai-elements/` 写入官方源码，同时安装所需 shadcn/ui 组件和依赖。如 `--overwrite` 不被当前 CLI 识别，先用 `apply_patch` 删除对应手写文件，再运行不带 `--overwrite` 的同一 CLI 命令。

- [ ] **Step 3: 验证文件确实来自 registry**

Run:

```bash
shasum src/components/ai-elements/conversation.tsx \
  src/components/ai-elements/message.tsx \
  src/components/ai-elements/prompt-input.tsx \
  src/components/ai-elements/reasoning.tsx
git diff -- src/components/ai-elements package.json package-lock.json
```

Expected: 4 个文件指纹与 Step 1 不同，源码包含 AI Elements 官方组合 API，例如 `ConversationScrollButton`、`MessageResponse`、`PromptInputTextarea` 和 `ReasoningTrigger`。

- [ ] **Step 4: 记录官方 API 与项目适配面**

Run:

```bash
rg -n "export (const|function|type)|export \{" src/components/ai-elements/{conversation,message,prompt-input,reasoning}.tsx
```

Expected: 能明确看到 `ConversationScrollButton`、`MessageResponse`、`PromptInputTextarea` 和 `ReasoningTrigger` 等官方导出，后续适配以实际导出为准。

- [ ] **Step 5: 提交官方组件与紧随其后的适配任务**

```bash
# 官方组件与 Task 5 的适配代码在构建通过后一起提交，
# 不创建已知无法构建的中间提交。
```

### Task 4: 建立路由和全局布局

**Files:**
- Create: `frontend/src/router/index.tsx`
- Create: `frontend/src/layouts/app-layout.tsx`
- Create: `frontend/src/components/navigation/main-navigation.tsx`
- Create: `frontend/src/views/route-error/index.tsx`
- Modify: `frontend/src/main.tsx`

- [ ] **Step 1: 实现主导航组件**

```tsx
import { Bell, Bot, BriefcaseBusiness, FileText, LineChart, Star } from 'lucide-react'
import { NavLink } from 'react-router-dom'

const items = [
  { label: '市场', icon: LineChart, disabled: true },
  { label: '自选', icon: Star, disabled: true },
  { label: '资讯', icon: FileText, disabled: true },
  { label: '持仓', icon: BriefcaseBusiness, disabled: true },
  { label: '策略与提醒', icon: Bell, disabled: true },
  { label: 'AI 研究', icon: Bot, to: '/ai-research' }
]

export function MainNavigation() {
  return <nav className="main-nav" aria-label="主导航">{items.map(({ label, icon: Icon, to, disabled }) =>
    disabled ? <button key={label} type="button" disabled title="功能规划中"><Icon size={16} />{label}</button>
      : <NavLink key={label} to={to!}><Icon size={16} />{label}</NavLink>
  )}</nav>
}
```

- [ ] **Step 2: 实现 AppLayout**

`AppLayout` 初始化 `useAuthentication`，渲染现有品牌、`MainNavigation`、搜索、通知和用户按钮，最后渲染：

```tsx
<Outlet context={{ authentication }} />
```

并导出：

```ts
export type AppOutletContext = {
  authentication: ReturnType<typeof useAuthentication>
}
```

- [ ] **Step 3: 实现错误页**

```tsx
import { Link } from 'react-router-dom'

export function RouteErrorView() {
  return <main className="route-error"><p>404</p><h1>页面未找到</h1><Link to="/ai-research">返回 AI 研究</Link></main>
}
```

- [ ] **Step 4: 建立路由表**

```tsx
import { Navigate, type RouteObject } from 'react-router-dom'

import { AppLayout } from '@/layouts/app-layout'
import { AiResearchView } from '@/views/ai-research'
import { RouteErrorView } from '@/views/route-error'

export const routes: RouteObject[] = [{
  element: <AppLayout />,
  errorElement: <RouteErrorView />,
  children: [
    { index: true, element: <Navigate replace to="/ai-research" /> },
    { path: 'ai-research', element: <AiResearchView /> },
    { path: '*', element: <RouteErrorView /> }
  ]
}]
```

- [ ] **Step 5: 缩减 main.tsx**

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider, createBrowserRouter } from 'react-router-dom'

import { routes } from '@/router'
import '@/styles.css'

const root = document.getElementById('app')
if (!root) throw new Error('App root not found.')

createRoot(root).render(<StrictMode><RouterProvider router={createBrowserRouter(routes)} /></StrictMode>)
```

- [ ] **Step 6: 运行路由测试**

Run: `npm test -- --run src/router/router.test.tsx`

Expected: 未实现 `AiResearchView` 前仍 FAIL，但 router 和 layout import 已解析。

### Task 5: 拆分 AI 研究页并适配官方 AI Elements

**Files:**
- Create: `frontend/src/views/ai-research/index.tsx`
- Create: `frontend/src/components/research/research-workspace.tsx`
- Create: `frontend/src/components/research/research-header.tsx`
- Create: `frontend/src/components/research/research-conversation.tsx`
- Create: `frontend/src/components/research/research-message-item.tsx`
- Create: `frontend/src/components/research/research-composer.tsx`
- Modify: `frontend/src/components/research/research-sidebar.tsx`
- Modify: `frontend/src/components/research/evidence-drawer.tsx`
- Delete: `frontend/src/App.tsx`

- [ ] **Step 1: 实现 AiResearchView 业务组装**

```tsx
import { useOutletContext } from 'react-router-dom'

import { ResearchWorkspace } from '@/components/research/research-workspace'
import { useEvidenceDrawer } from '@/hooks/use-evidence-drawer'
import { useResearchAttachments } from '@/hooks/use-research-attachments'
import { useResearchHistory } from '@/hooks/use-research-history'
import { useResearchSession } from '@/hooks/use-research-session'
import type { AppOutletContext } from '@/layouts/app-layout'

export function AiResearchView() {
  const { authentication } = useOutletContext<AppOutletContext>()
  const history = useResearchHistory()
  const session = useResearchSession(authentication.identity)
  const attachments = useResearchAttachments(session.sessionId, authentication.identity)
  const evidence = useEvidenceDrawer()

  return <ResearchWorkspace authentication={authentication} history={history} session={session} attachments={attachments} evidence={evidence} />
}
```

- [ ] **Step 2: 实现 ResearchMessageItem**

使用官方组件 API：

```tsx
<Message from={message.role}>
  <MessageContent>
    {message.thinking && <Reasoning isStreaming={message.status === 'streaming'}>
      <ReasoningTrigger />
      <ReasoningContent>{message.thinking}</ReasoningContent>
    </Reasoning>}
    <MessageResponse>{message.content || '正在整理研究结果…'}</MessageResponse>
  </MessageContent>
</Message>
```

引用序号、复制与反馈作为 `ResearchMessageItem` 自身的领域操作区，不写入官方组件文件。

- [ ] **Step 3: 实现 ResearchConversation**

```tsx
<Conversation>
  <ConversationContent>
    {messages.map((message) => <ResearchMessageItem key={message.id} message={message} onCitation={onCitation} />)}
  </ConversationContent>
  <ConversationScrollButton />
</Conversation>
```

- [ ] **Step 4: 实现 ResearchComposer**

通过官方 `PromptInput`、`PromptInputTextarea`、`PromptInputFooter`、`PromptInputTools`、`PromptInputButton` 和 `PromptInputSubmit` 组合输入区。`onSubmit` 仅调用页面传入的语义化命令：

```ts
onSend(value, attachments.flatMap((item) => item.assetId ? [item.assetId] : []))
```

文件选择仍由 `useResearchAttachments.addFiles` 处理，不使用 AI SDK 上传协议。

- [ ] **Step 5: 实现 ResearchWorkspace**

`ResearchWorkspace` 管理移动端历史栏的局部开关，组合：

```tsx
<main className="workspace">
  <ResearchSidebar {...sidebarProps} />
  <section className="research-main">
    <ResearchHeader title={history.activeThread?.title} isOnline={session.isOnline} />
    <ResearchConversation messages={session.messages} onCitation={openCitation} />
    <ResearchComposer {...composerProps} />
  </section>
  <EvidenceDrawer {...drawerProps} />
</main>
```

- [ ] **Step 6: 删除 App.tsx 并修正所有 import**

Run:

```bash
rg -n "from ['\"]\./App|from ['\"].*lib/cn|components/ai-elements" src
```

Expected: 不存在 `App.tsx` 或 `lib/cn` import；AI Elements import 只来自研究适配组件。

- [ ] **Step 7: 运行路由与全量测试**

Run:

```bash
npm test -- --run src/router/router.test.tsx
npm test
```

Expected: 路由测试和原有 27 个测试全部 PASS。

- [ ] **Step 8: 提交路由与页面拆分**

```bash
git add frontend/src frontend/package.json frontend/package-lock.json
git commit -m "refactor(frontend): split AI research into routed views"
```

### Task 6: 样式、SPA fallback 与文档收尾

**Files:**
- Modify: `frontend/src/styles.css`
- Verify: `frontend/nginx.conf`
- Modify: `frontend/README.md`

- [ ] **Step 1: 对齐 AI Elements 与 InvestWise 设计令牌**

在 `styles.css` 保留 `@import "tailwindcss"`，并保留 CLI 生成的 shadcn CSS variables。业务样式通过适配组件的 `className` 与定向 class 控制，不在官方组件中写 InvestWise 业务样式。

- [ ] **Step 2: 验证 Nginx SPA fallback**

Run:

```bash
rg -n "try_files \\$uri \\$uri/ /index.html" nginx.conf
```

Expected: 命中 `location /` 中的 SPA fallback，无需修改 Nginx。

- [ ] **Step 3: 更新 README**

记录：

- `/ai-research` 路由和 `/` 重定向。
- `layouts/`、`views/`、`components/research/`、`hooks/`、`services/`、`utils/` 边界。
- AI Elements 组件由 `npx ai-elements@latest` 从官方 registry 生成。
- 项目保留自定义 SSE 后端协议，不依赖 AI SDK `useChat` 发送消息。

- [ ] **Step 4: 运行静态边界检查**

Run:

```bash
rg -n "fetch\(|localStorage|parseSseEvent" src/components src/views src/layouts
rg -n "function App|<App" src
```

Expected: 第一条无输出；第二条仅允许 `AppLayout` 符合，不存在原 `App` 页面。

- [ ] **Step 5: 运行完整验证**

Run:

```bash
npm test
npm run build
git diff --check
```

Expected: 全部 PASS，Vite 生产构建无超过 500 kB 的单 chunk 告警。

- [ ] **Step 6: 浏览器验证**

启动 `npm run dev`，检查：

1. `/` 自动进入 `/ai-research`。
2. 桌面端导航、历史栏、对话和输入区布局与现有设计一致。
3. 390×844 下移动历史按钮可展开侧边栏。
4. 引用序号打开证据抽屉并高亮对应证据。
5. 控制台无 error 或 warning。

- [ ] **Step 7: 提交收尾变更**

```bash
git add frontend/src/styles.css frontend/README.md frontend/nginx.conf
git commit -m "docs(frontend): document routed AI Elements architecture"
```

### Task 7: 最终审核

**Files:**
- Review: all files changed from `dev...HEAD`

- [ ] **Step 1: 确认 CLI 来源和目录边界**

Run:

```bash
git diff --name-status dev...HEAD
rg -n "fetch\(|localStorage|parseSseEvent" frontend/src/components frontend/src/views frontend/src/layouts
test ! -f frontend/src/App.tsx
test -f frontend/src/router/index.tsx
test -f frontend/src/views/ai-research/index.tsx
test -f frontend/src/utils/cn.ts
```

Expected: 页面结构符合设计，视图/组件中不存在网络或存储细节。

- [ ] **Step 2: 在干净依赖下验证**

Run:

```bash
npm ci
npm test
npm run build
```

Expected: 安装、测试和构建全部 PASS。

- [ ] **Step 3: 确认工作树干净**

Run: `git status --short`

Expected: 无输出。
