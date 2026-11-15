# Tool 容错与官方流式协议改造实施记录

**目标：** 修复 Tool 参数生成失败导致整条 Graph 返回 500 的问题，并用 LangGraph 官方多模式流与官方前端 Hook 持续输出公开任务进度和最终答案 Token。

**架构选择：** 不迁移到独立 LangGraph Agent Server。现有 FastAPI 继续负责 JWT、租户 RBAC、幂等 Turn、附件、审批和 PostgreSQL Message/Run 生命周期；Backend 同时提供 LangGraph Agent Streaming Protocol 与独立 AG-UI 适配端点，Frontend 使用 `@langchain/langgraph-sdk/react` 的 `useStream + FetchStreamTransport`。这样可复用官方消息拼接和流生命周期，同时避免引入第二套会话持久化事实源。

**技术栈：** Python、LangGraph、LangChain、FastAPI SSE、`ag-ui-protocol`、DeepSeek OpenAI-compatible API、React、TypeScript、`@langchain/langgraph-sdk`、Pytest、Vitest。

## 已实施

- [x] Tool Agent 将未生成 Tool Call、错误工具名转换为 `business_failure` Observation。
- [x] Planner 明确单工具任务的必需参数来源与复数实体拆分约束。
- [x] Graph 使用 `updates + messages + custom` 官方多模式流。
- [x] 各业务节点通过 `get_stream_writer()` 输出结构化进度，Node Trace 仅用于观测，不进入产品界面。
- [x] DeepSeek Provider 直接消费原生 SSE，将 `reasoning_content` 与最终 `content` 分流，避免 `langchain-openai` 忽略 DeepSeek 扩展字段。
- [x] FastAPI 新增兼容 Agent Streaming Protocol 的流式端点，并保留原有业务生命周期。
- [x] Frontend 使用官方 `useStream + FetchStreamTransport`，删除自建 SSE 解析器。
- [x] SDK 按稳定 Message ID 拼接答案分片；原生 Token 已发送时不再执行字符级二次伪流式。
- [x] 前端按阶段展示公开进度，并区分“正在研究、研究完成、研究失败”。
- [x] Backend 新增独立 AG-UI 适配端点，覆盖标准事件编码、Interrupt outcome 和 `RunAgentInput.resume`。
- [x] AG-UI 适配器将推理增量输出为 `REASONING_START/MESSAGE_CONTENT/REASONING_END` 标准事件。

## 协议边界

```text
DeepSeek reasoning -> LangGraph custom        -> onCustomEvent -> 推理文本
DeepSeek content   -> LangGraph custom        -> SDK messages  -> 最终答案 Token
LangGraph progress -> Agent Protocol custom   -> onCustomEvent -> 工具/阶段活动
LangGraph updates  -> Backend 累计 State      -> 业务事务      -> Message/Run
Node Trace          -> Trace 持久化             -> 仅观测调试
```

AG-UI 审批恢复复用现有审批 API 的 claim、身份校验与 `Command(resume=...)`，不新建第二套审批状态机。当前 Frontend 仍消费 LangGraph SDK 协议；AG-UI 端点作为独立兼容边界供后续客户端迁移或第三方集成。

## 验收项

- [x] Tool Agent 与 Planner 单元回归。
- [x] 会话流验证公开进度先于答案、答案 Token 先于最终事件。
- [x] 协议端点验证 `custom/messages` 事件及稳定 Message ID。
- [x] 验证原生答案 Token 不被二次切片重复发送。
- [x] Backend 全量测试与 Ruff。
- [x] Frontend Vitest 与生产构建。
- [ ] 本地真实 Provider/MCP 冒烟测试。
