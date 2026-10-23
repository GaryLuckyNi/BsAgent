# Langfuse 调度输入输出与路由解释

## 已确认范围

用户已确认补充 `task_scheduler` 和任务分发处的 Langfuse Input/Output。只增加观测，不改变路由优先级、工具执行、预算或 Planner 评审策略。

## 设计

- 沿用 SDK 已有的 `task_scheduler` 节点观测，通过现有导出投影开放安全的调度状态和局部更新；空更新 `{}` 也应可见。
- 在 `_dispatch_after_scheduler` 内增加 `dispatch_task_scheduler` span。每个实际返回分支同时记录稳定的 `reason`，输出仅包含目标节点和任务标识，不序列化 `Send.arg` 的完整 State。
- 输入摘要包括计划、结果状态、依赖就绪情况、Planner 评审状态及预算、Skill 工作流状态、待审批/用户确认标记。不采集工具结果正文、用户确认正文、租户或权限凭证。
- 观测构建、SDK 更新失败不影响业务结果；继续遵守 `metadata_only`、脱敏和长度限制。不会新增 Generation。

## 验收

真实 LangGraph + Langfuse SDK 的内存导出测试覆盖首次派发、工具成功后待评审、已评审结束；断言 span 父子关系、Input/Output、路由原因、无敏感正文及无新增模型调用。另覆盖审批、用户确认、预算耗尽、Skill 分支和观测故障隔离。
