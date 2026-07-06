# BsAgent Frontend

## 目标

`frontend/` 是 BsAgent 的真实 Agent 运行页面，用于发送用户消息、展示 Agent 回复，并根据后端返回的 `trace_id` 拉取节点执行 trace。

页面不包含 mock 响应、不使用本地假 trace，也不会在接口失败时伪造成功结果。

## 接口

默认通过 Vite dev server 代理到 BsAgent 后端：

```text
POST /api/sessions/{session_id}/messages
POST /api/sessions/{session_id}/messages/stream
GET  /api/traces/{trace_id}
GET  /health
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
