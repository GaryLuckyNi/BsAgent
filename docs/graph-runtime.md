# Graph Runtime

> 2026-10-24：默认关闭的确定性 `permission_recovery` 节点与证据/恢复观测已接线。普通主链保持不变；状态、受控测试与启用门槛见[第三检查点](./acceptance/2026-10-24-runtime-capability-recovery.md)。

```text
实际批次 Send（先固定成员） → worker 结果/恢复事项 → Scheduler 收敛并封存
→ UserInputGate（完整清单、版本/摘要、持久化决定）
→ Scheduler → permission_recovery（只执行批准的替代搜索）
→ Scheduler → 原 Planner 证据评审 → 继续或合成
```

Gate 不调用工具，恢复不重跑原 MCP 或工具选择模型。来源变化本身不强制 revise；partial/search_model 和原数据缺失事实随观察进入评审。正常调用被同批 running 保护本地拦住时，只有尚无远端回执且参数快照未变的等待可在同批结算后继续一次；真实 running/unknown 不因此放开。

## 主链路

BsAgent 使用单一动态任务图，不再按固定的知识问答、工具动作或 A 股模板分叉：

```text
catalog_guard
-> attachment_analyzer
-> intent_router
-> planner_agent
-> task_scheduler
   -> skill_agent    \
   -> tool_agent      +-> task_scheduler
   -> analysis_agent /
-> synthesizer
-> verifier
-> approval_coordinator
-> final_response
```

`intent_router` 判断是否属于 Admin 已启用的投研范围，并在不改变语义的前提下生成 `goal`。超出范围时直接进入答案合成，不规划或调用工具。

## 动态规划

Planner 将 Admin 已按 `domain/subdomain` 过滤的全部轻量 Skill 卡片按名称稳定披露给模型，不再对 Skill 做文本检索；Planner 也不检索或绑定普通工具。模型按用户目标生成最多 12 个任务，保留所需证据和完成条件：

```json
{
  "tasks": [
    {"id": "market", "task_goal": "取得目标公司行情", "execution_type": "tool", "completion_criteria": ["标明数据期间与来源"], "depends_on": [], "skill_name": null},
    {"id": "interpret", "task_goal": "依据行情分析变化", "execution_type": "analysis", "completion_criteria": ["区分事实和推断"], "depends_on": ["market"], "skill_name": null}
  ]
}
```

`execution_type=analysis` 路由到 Analysis Agent，仅使用已有材料；`tool` 路由到 Tool Agent，由模型从检索候选中选择一次工具调用或直接回答。绑定已披露 Skill 的任务路由到 Skill Agent。三个执行节点都会收到 `completion_criteria` 和有长度限制的用户材料。修订计划可通过 `evidence_task_ids` 显式引用历史成功结果；Planner 校验引用后才把证据交给执行节点。旧任务省略 execution_type 时默认 tool；这只是字段默认值，不代表旧 Checkpoint 可以跨策略版本恢复。

Planner 保留模型提出的必要分析，不按拓扑静默删任务。结构、依赖和能力引用错误允许一次模型修复；无法修复则明确报错。

## 调度、反馈和版本

Scheduler 只派发依赖已成功的任务，无依赖任务通过 LangGraph Send 并行执行。批次结果产生后，planner 读取当前计划、原始问题和观察，自主选择：

- continue：保持计划，推进仍可执行任务。
- revise：提交完整的新活动计划，明确复用已完成任务。
- finish：结束研究，可保留证据限制，不等同于所有目标均成功。

业务失败不自动触发修订，工具成功也会进入模型反馈。代码只校验可执行性、依赖、授权和预算。无进展 continue 被反馈给模型修复，不由代码生成替代业务计划。

当 `revise` 的任务列表与现计划完全相同（例如只改摘要）时，本地规范为 `continue`（仍有就绪任务）或 `finish`（无可推进任务），保留未核实问题，不再请求纠错、不消耗修订次数、不重命名任务。任务次序、目标、依赖或验收条件发生变化仍按真实修订处理。

取证评审可以返回 `{"action":"finish","reason":"取证阶段结束","synthesis_task_id":"interpret"}`，显式将最后一次研判交给 Synthesizer。必须是唯一未完成、依赖成功、无后续消费者的就绪 `analysis`，且不能绑定 Skill；未知 ID、失败依赖、仍有工具或其他待办均被拒绝。Synthesizer 接收该任务目标、验收条件、显式引用的历史成功证据及用户材料，启用推理，一次完成分析和最终答复；完成结果只保存对最终答复的引用，不再复制整篇正文。交接路径的推理与正文共用 6144 输出 Token 预算，普通问答正文以800至1200汉字为目标；Planner 可见预算，需要更大输出的长篇报告不交接。普通合成仍保留原有 16384 上限。中间分析仍独立执行，取证后的 Planner 评审不省略，Planner 本身不输出用户答案。

模型输入使用确定性投影，不新增压缩模型：Planner/Analysis 已单列问题时排除用户材料中的当前消息副本；Tool Agent 只用一份有长度限制的会话材料。Planner 评审与合成将 JSON 结果解析成对象，并通过共享来源表去重；评审保留 query 以识别偏题，合成不接收重复 query、调度 action、重复缺口或空证据字段。未知结果字段原样保留，原始 State、ToolMessage artifact 和调用审计不改写。

反馈输出若连续两次不符合契约，Planner 记录 `planner_feedback_invalid` 并停止后续派发，由 Synthesizer 基于已获得的材料说明未核实目标。该停止是 Harness 故障边界，不伪装成模型选择的 finish；初始计划无效仍按明确错误处理。

计划修订仅在批次收敛后应用。已完成任务不能改写目标；复用任务保留 ID，新任务获得版本前缀，旧计划进入 plan_history。任务结果和调用记录按可信 ID 合并；检索披露列表及按任务标识的发现事件使用 reducer 合并，批次收敛时统计检索次数。

初始计划和修订共受 min(BS_AGENT_PLANNER_MAX_ITERATIONS, 2) 限制；反馈最多 24 次，不因修订预算耗尽而剥夺模型 continue/finish 的选择。超过反馈预算记录 planner_stop_reason，合成答案必须说明未完成目标。图仍受 BS_AGENT_GRAPH_RECURSION_LIMIT 超步上限保护；默认值为 128，可容纳契约允许的 12 个串行任务及其反馈步骤。审批恢复使用同一上限。

工具错误作为观察和 ToolMessage 保存；不再通过重跑整个 Tool Agent 节点重新选择工具和参数。当前不新增自动 Provider 重试，连接恢复、幂等重试为后续独立工作。其他现有节点的基础设施重试保持原有机制。

策略版本为 model-feedback-v1。审批恢复要求归属、目录摘要和策略版本一致；旧版本 Checkpoint 拒绝继续，需重新发起请求。

## Store 与 Checkpointer

```python
graph.compile(checkpointer=checkpointer, store=store)
```

- `PostgresSaver` 保存短期 Graph State、中间消息、Checkpoint 历史与 Interrupt，用于断点续传和审批恢复。
- `PostgresStore` 保存跨会话长期偏好，Planner 和 Synthesizer 通过 LangGraph `Runtime.store` 按需读取。
- 业务 PostgreSQL 表保存 Message、Run、Candidate 和 Approval 审计投影，不替代 Checkpointer 或 Store。

正常审批恢复只使用 `Command(resume=...)`。工具目录摘要随 State 写入 Checkpoint，恢复时目录或执行绑定变化则拒绝继续旧调用。

## 流式协议

Graph 使用 LangGraph 官方多模式流：

```python
graph.stream(
    state,
    config=config,
    stream_mode=["updates", "messages", "custom"],
)
```

- `updates`：合并节点返回的局部 State，并作为最终业务持久化依据。
- `messages`：只将 `synthesizer` 的 LLM 消息分片作为最终答案 Token 对外发送；Intent、Planner 等内部模型调用不会作为答案泄露。
- `custom`：节点通过 `get_stream_writer()` 发送“理解问题、制定计划、获取证据、分析证据、生成回答”等公开执行进度，不包含私有思维链、工具参数或 Node Trace。

Backend 保留现有 FastAPI、JWT、租户 RBAC、幂等 Turn 和 PostgreSQL Message/Run 生命周期，同时提供两条彼此隔离的协议入口：

- `/api/sessions/{session_id}/messages/protocol-stream` 面向当前 Frontend，使用 LangGraph SDK 的 `useStream + FetchStreamTransport` 消费 `messages/custom` 事件。
- `/api/ag-ui` 面向 AG-UI 客户端，接收官方 `RunAgentInput`，输出 `RUN_*`、`STEP_*`、`ACTIVITY_SNAPSHOT` 和 `TEXT_MESSAGE_*` 事件。

两条入口复用同一业务执行函数，不重复创建 Message、Run、Checkpoint 或审批事实源。客户端提交的 AG-UI `tools/context` 不会绕过服务端冻结的 Tool Catalog、Skill Registry 与 RBAC。

审批发生时，AG-UI 端点以 `RUN_FINISHED` 的 `interrupt` outcome 返回脱敏审批信息；客户端使用 `RunAgentInput.resume` 提交决定。适配层校验租户、会话、Run 与 Interrupt 身份后，复用现有审批 claim、`Command(resume=...)` 和 PostgreSQL Checkpointer。当前 Graph 每次只恢复一个活动 Interrupt；连续审批会返回新的 interrupt outcome。

Node Trace 仍只进入观测与调试存储，不发送到产品端。若最终答案没有经过 LLM Token 流，例如范围外回复或确定性错误回复，Backend 才对最终文本执行兼容性分片。
