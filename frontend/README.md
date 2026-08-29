# InvestWise Frontend

## 目标

`frontend/` 是 InvestWise 的面向投资者的 AI 研究工作台。当前聚焦“AI 研究”业务，提供研究历史、流式回答、图片输入与可追溯证据。市场、自选、持仓和策略等菜单作为后续产品扩展入口保留。

新会话会展示一条静态研究示例，用于说明引用与证据抽屉交互。用户实际提问仍调用后端 SSE 接口，接口失败时不伪造成功结果。

## 前端边界

页面组件只负责组合与展示，可复用业务单元收口在 `src/hooks/`：

- `useResearchSession`：会话恢复、SSE 发送、取消与消息状态。
- `useResearchHistory`：研究历史与激活会话。
- `useResearchAttachments`：图片校验、上传和预览生命周期。
- `useEvidenceDrawer`：证据抽屉开关和引用索引定位。
- `useAuthentication`：OIDC 初始化、身份映射和登录/退出命令。

网络协议集中在 `src/services/research-api.ts`，组件内不直接调用 `fetch`、解析 SSE 或读写 `localStorage`。

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
