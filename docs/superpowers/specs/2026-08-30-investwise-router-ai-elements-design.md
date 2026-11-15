# InvestWise 路由与 AI Elements 工程化改造设计

## 背景

当前 InvestWise 前端已实现 AI 研究工作台，但存在两个结构性问题：

1. `App.tsx` 同时承担全局导航、页面布局、业务组装和消息展示，不利于后续增加市场、自选、持仓和量化策略等页面。
2. `components/ai-elements/` 为项目内手写的仿 AI Elements 组件，未使用官方 CLI 提供的组件源码。

本次改造不调整已确认的产品视觉和 AI 研究交互，只改善组件来源、路由管理和代码组织。

## 目标

- 使用 `npx ai-elements@latest` 安装官方 AI Elements 组件，删除项目自行实现的同名组件。
- 使用 React Router 管理页面，为后续业务菜单独立发展提供稳定入口。
- 将全局布局、页面、业务组件、hooks、协议和工具函数分层。
- 保留现有 SSE 会话、证据抽屉、附件、OIDC 和响应式能力。
- 不为尚未实现的业务创建空页面或占位路由。

## 路由设计

采用 React Router 的 `createBrowserRouter` 和 `RouterProvider`。

| 路径 | 行为 |
| --- | --- |
| `/` | 重定向到 `/ai-research` |
| `/ai-research` | 渲染 AI 研究页面 |
| 其他路径 | 渲染简洁的路由错误页 |

市场、自选、资讯、持仓和策略与提醒菜单继续保持禁用，不配置对应路由。Nginx 保持 SPA fallback，直接访问 `/ai-research` 时回退到 `index.html`。

## 目录设计

```text
src/
├── main.tsx
├── router/
│   └── index.tsx
├── layouts/
│   └── app-layout.tsx
├── views/
│   ├── ai-research/
│   │   └── index.tsx
│   └── route-error/
│       └── index.tsx
├── components/
│   ├── ai-elements/
│   ├── ui/
│   ├── navigation/
│   └── research/
├── hooks/
├── services/
├── domain/
└── utils/
```

边界约定：

- `main.tsx` 只挂载 `RouterProvider`。
- `router/` 只定义路由、重定向和页面级错误边界。
- `layouts/` 负责全局产品框架，通过 `Outlet` 渲染页面。
- `views/` 负责页面级业务组装，不实现通用视觉基础组件。
- `components/research/` 负责 AI 研究领域的可复用展示与适配组件。
- `components/ai-elements/` 和 CLI 依赖的 `components/ui/` 由官方 CLI 生成，页面业务不写入这些组件。
- `hooks/` 继续承担会话、历史、附件、证据和鉴权状态。
- `services/` 继续承担 HTTP 与 SSE 协议。
- `utils/` 存放无业务状态的通用 TypeScript 工具函数，例如样式合并、格式化和纯函数转换。
- `types/` 只在类型被多个业务模块复用时建立；`typings/` 只在存在全局声明或第三方模块扩展时建立。不创建空目录。

## AI Elements 集成

1. 配置 Vite 工程的 `@/` 路径别名与 shadcn 兼容配置。
2. 使用 `npx ai-elements@latest` 安装实际使用的官方组件，包括 `conversation`、`message`、`prompt-input` 和 `reasoning`。
3. 删除原手写同名组件。
4. 在 `components/research/` 中建立业务适配层，将现有 `ResearchMessage`、引用索引、附件状态和 SSE 状态转换为 AI Elements 组件 props。
5. 不引入 AI SDK 的消息协议替换项目已有 SSE 协议，避免改变后端契约。

## 页面与组件拆分

`AiResearchView` 只组合页面级状态，具体展示拆分为：

- `ResearchWorkspace`：研究页面主布局。
- `ResearchHeader`：标题、说明与服务状态。
- `ResearchConversation`：消息序列和滚动容器。
- `ResearchMessageItem`：单条消息、思考过程、引用与反馈操作。
- `ResearchComposer`：将附件和会话 hooks 适配到官方 `PromptInput`。
- `ResearchSidebar`：研究历史。
- `EvidenceDrawer`：证据列表与引用定位。

`AppLayout` 仅包含品牌、主导航、用户入口和 `Outlet`。

## 数据流

1. `AiResearchView` 初始化鉴权、会话、历史、附件和证据 hooks。
2. View 将状态与语义化命令传入研究业务组件。
3. 研究业务组件把数据映射为官方 AI Elements 组件所需 props。
4. hooks 通过 `research-api.ts` 访问后端，组件不直接使用 `fetch`、SSE 解析或 `localStorage`。

## 错误处理

- 路由加载错误由 `RouteErrorView` 处理。
- 会话、附件和鉴权错误继续由对应 hook 返回，由 `ResearchComposer` 附近的统一错误区展示。
- AI Elements CLI 如果不能直接识别 Vite，先补齐 shadcn 配置后重新运行 CLI，不回退到手写同名组件。

## 测试与验证

- 保留现有领域、SSE、会话、鉴权和证据 hook 测试。
- 增加路由测试：`/` 重定向、`/ai-research` 渲染和未知路由错误页。
- 增加 AI 研究页面烟雾测试，确认官方 AI Elements 组件能与现有 hooks 共同渲染。
- 执行全量 `npm test`、`npm run build` 和浏览器桌面/移动端检查。
- 验证 Nginx 直接访问 `/ai-research` 的 SPA fallback。

## 非目标

- 本次不实现市场、自选、资讯、持仓和量化策略页面。
- 不替换后端 SSE 事件格式。
- 不调整已确认的 InvestWise 视觉设计。
- 不为尚未出现的复用需求创建空的 `types/` 或 `typings/` 目录。
