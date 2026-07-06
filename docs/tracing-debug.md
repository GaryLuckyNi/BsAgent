# Tracing 与调试能力

## 当前能力

第一版调试能力使用本地 Trace Store，默认开启，记录每个节点的输入/输出 state summary。

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
- `planner_agent`：objective、execution_steps、retrieval、tool_policy。
- `skill_agent`：selected_skill、requested_tools。
- `retriever_agent`：tool_calls、evidence coverage、evidence ids。
- `analysis_agent`：findings、missing_information、confidence。
- `synthesizer`：draft_answer claim_count、cited_evidence_ids、response_policy。
- `verifier`：verdict、faithfulness_score、citation_errors。
- `final_response`：最终响应。

## 脱敏规则

state summary 会递归脱敏这些 key：

- `api_key`
- `apikey`
- `token`
- `secret`
- `password`
- `authorization`

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
