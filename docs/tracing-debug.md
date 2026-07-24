# Tracing 与调试能力

## 当前能力

第一版调试能力使用本地 Trace Store，默认开启，记录每个节点的输入/输出白名单 state summary。

本地 artifact 是脱敏摘要，不保存完整 state。它用于全链路测试时快速观察每个节点做了什么。

## 本地 Trace 查询

发送一次会话请求后，响应会包含 `trace_id`：

```bash
curl -X POST http://127.0.0.1:8000/api/sessions/e2e_s1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "tenant_1",
    "user_id": "user_1",
    "message": "RAG 是什么？"
  }'
```

然后查询：

```bash
curl http://127.0.0.1:8000/api/traces/<trace_id>
```

返回内容包含：

- `events`：节点执行顺序、状态、耗时、输入 artifact ref、输出 artifact ref。
- `artifacts`：每个 artifact ref 对应的脱敏 state summary。

你可以重点看这些节点的 output summary：

- `intent_router`：intent、risk_level、candidate_skill。
- `planner_agent`：execution_mode、步骤数量、retrieval 策略、允许的工具名和请求 call ID。
- `skill_agent`：Skill 名称、版本、状态、允许的工具名和请求 call ID。
- `retriever_agent`：工具状态/错误码、evidence coverage、evidence IDs。
- `analysis_agent`：finding 数量、缺失信息数量、confidence。
- `synthesizer`：正文长度、claim 数量、cited evidence IDs、confidence。
- `verifier`：verdict、faithfulness score、错误/风险数量。
- `final_response`：正文长度、引用 ID 和审批状态，不包含最终回答正文。

## 脱敏规则

Trace 首先使用固定字段白名单，不保存用户消息、工具参数/结果、analysis 正文、草稿正文或最终回答正文；随后还会递归脱敏敏感 key。异常事件不保存原始 `str(exc)`：

- `event.error` 仅为稳定的 `error_code:ExceptionType`。
- metadata 仅保留 `error_code`、`exception_type` 和服务端白名单 message。
- Store/API 防御性清洗 URI userinfo/query、Authorization Basic/Bearer、Proxy-Authorization、JWT、冒号/等号凭证赋值，并截断异常字段。

RAG Provider 的 502/503 等已归一化失败会作为成功提交的节点状态出现：Retriever 事件本身为 success，工具记录和 State error 带业务错误码，最终返回安全错误回答。只有 ready/call ID/指纹等执行契约错误才记录失败节点事件。

当前仍是进程内存储，服务重启后会丢失。后续可接数据库或对象存储。

## 当前边界

当前版本还没有做：

- artifact 持久化数据库表
- trace retention 策略
- checkpoint id 与 trace artifact 绑定
- 失败节点 replay
- 外部观测平台集成
- OpenTelemetry exporter

这些属于下一层工业化增强。
