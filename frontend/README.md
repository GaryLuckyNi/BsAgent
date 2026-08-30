# InvestWise Frontend

## 目标

`frontend/` 是 InvestWise 的面向投资者的 AI 研究工作台。当前聚焦“AI 研究”业务，提供研究历史、流式回答、图片输入与可追溯证据。市场、自选、持仓和策略等菜单作为后续产品扩展入口保留。

新会话会展示一条静态研究示例，用于说明引用与证据抽屉交互。用户实际提问仍调用后端 SSE 接口，接口失败时不伪造成功结果。

## 前端边界

路由使用 React Router 的 `createBrowserRouter`：

- `/` 重定向到 `/ai-research`。
- `/ai-research` 是当前唯一开放的业务页面。
- 其他未知路径进入统一错误页；尚未开发的主菜单不创建占位路由。

目录职责：

- `src/layouts/`：产品级框架和 `Outlet`。
- `src/views/`：页面级业务组装。
- `src/components/research/`：AI 研究领域展示与组件适配。
- `src/components/ai-elements/`：由官方 AI Elements registry 生成的组件源码。
- `src/components/ui/`：AI Elements 依赖的 shadcn/ui 基础组件。
- `src/hooks/`：可复用业务状态和语义化命令。
- `src/services/`：HTTP 和 SSE 协议。
- `src/utils/`：不带业务状态的通用 TypeScript 工具。

业务 hooks 包括：

- `useResearchSession`：会话恢复、SSE 发送、取消与消息状态。
- `useResearchHistory`：研究历史与激活会话。
- `useResearchAttachments`：图片校验、上传和预览生命周期。
- `useEvidenceDrawer`：证据抽屉开关和引用索引定位。
- `useAuthentication`：OIDC 初始化、身份映射和登录/退出命令。

网络协议集中在 `src/services/research-api.ts`，业务 view、layout 和 research 组件不直接调用 `fetch`、解析 SSE 或读写 `localStorage`。

## AI Elements

`Conversation`、`Message`、`PromptInput` 和 `Reasoning` 由官方 CLI 安装：

```bash
npx ai-elements@latest add conversation message prompt-input reasoning
```

项目保留自有 SSE 后端契约，在 `components/research/` 中将领域消息适配为 AI Elements props，不使用 AI SDK `useChat` 替换后端通信。为控制投资研究页的体积，官方 `Streamdown` 当前只启用 CJK 插件，未启用尚未使用的代码高亮、数学公式和 Mermaid 插件。

## 接口

默认通过 Vite dev server 代理到 BsAgent 后端：

```text
POST /api/sessions/{session_id}/messages/stream
GET  /api/sessions/{session_id}/messages
POST /api/assets/images
GET  /health/ready
```

页面默认使用 `/messages/stream` 读取 `text/event-stream` 响应，并随着 `answer_delta` 事件逐步渲染 Agent 回复。请求失败时不会伪造本地答案。

默认后端地址：

```text
http://127.0.0.1:8000
```

如需直连其它后端地址，可以设置：

```bash
VITE_AGENT_API_BASE_URL=http://127.0.0.1:8000
```

## 启动

```bash
cd frontend
npm install
npm run dev
```

默认前端端口：

```text
http://127.0.0.1:5175
```

## 构建

```bash
cd frontend
npm run build
```
