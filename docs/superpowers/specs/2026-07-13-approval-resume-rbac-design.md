# 审批恢复与 Mock RBAC 设计

## 背景

当前 Agent Graph 会把需要审批的请求路由到 Synthesizer，随后生成一个 `pending` 状态的最终响应。图没有调用 LangGraph `interrupt()`，审批接口也没有持久化、批准、拒绝或恢复能力，因此现状不是断点续传。

当前 checkpoint 的 `thread_id` 使用 `session_id`，会让同一会话中的多个 Run 共享恢复地址。审批恢复必须定位到一次具体执行，因此 checkpoint 线程需要改为 Run 级。

## 目标

1. 工具调用前使用 LangGraph 原生 `interrupt()` 暂停 Graph。
2. 审批记录持久化并绑定工具名称、版本、规范化参数哈希和 checkpoint thread。
3. 审批后使用 `Command(resume=...)` 从原断点恢复，不重新执行上游 LLM 节点。
4. Tool Execution Gateway 只接受与审批记录完全一致的调用哈希。
5. 引入可替换 `RbacProvider`，第一版使用 Mock RBAC 验证调用、查看审批、审批决策和工具权限。

## 核心边界

```text
Session API
  -> RbacProvider.resolve_principal
  -> Graph(thread_id=run_id)
  -> Approval Gate
       -> interrupt(approval payload)
  -> ApprovalRepository 持久化 pending

Approval API
  -> RbacProvider.authorize("approval:decide")
  -> ApprovalRepository 原子状态迁移
  -> Command(resume=decision, update=current_permissions)
  -> Approval Gate 校验 hash
  -> ToolExecutionGateway 再次校验 hash + permission_scope
  -> Tool Provider
```

## Run 与 Checkpoint

- `session_id`：多轮会话归属，不作为审批恢复地址。
- `request_id/run_id`：一次 Graph 执行的稳定标识。
- `checkpoint_thread_id`：等于 `run_id`。
- `approval_id`：由 `run_id + tool_call_hash` 生成稳定标识，节点重放不会创建重复审批。

同一 Session 可同时存在多个等待审批的 Run，不会互相覆盖 checkpoint。

## 工具调用指纹

指纹输入采用规范化 JSON：

```json
{
  "tool_name": "mcp.mock.stock.some_write_tool",
  "tool_version": "mcp-stdio-v1",
  "arguments": {"symbol": "600519.SH"}
}
```

使用 UTF-8、字段排序、紧凑分隔符计算 SHA-256。审批恢复时，Approval Gate 和 Tool Execution Gateway 都重新计算；名称、版本或参数任一变化都会拒绝执行。

## 审批记录

新增 `approval_records`：

- `approval_id`
- `run_id`
- `session_id`
- `tenant_id`
- `request_user_id`
- `approval_type`
- `tool_name`
- `tool_version`
- `arguments_hash`
- `payload_json`
- `checkpoint_thread_id`
- `status`
- `decision`
- `resume_claim_token`
- `resume_attempts`
- `last_resume_error`
- `resume_claimed_by`
- `resume_claimed_at`
- `decided_by`
- `created_at`
- `decided_at`

状态机为 `pending -> resuming -> approved|rejected|resume_failed`，其中 `resume_failed` 只允许使用相同 `decision` 再次进入 `resuming`。`resuming` 通过数据库 CAS claim token 保证同一时刻只有一个请求可以恢复 Graph；重复提交相同终态决定返回当前结果，冲突决定返回 `409`。首次业务决定的 `decision/decided_by/decided_at` 不可被恢复重试人覆盖，最近恢复执行人和失败证据使用独立字段记录。

## RBAC

定义 `RbacProvider` 协议，业务层只依赖：

- `resolve_principal(tenant_id, user_id)`
- `has_permission(principal, permission)`
- `tool_scopes(principal)`

Mock 角色：

- `analyst`：`agent:invoke`、`rag:retrieve`、`mcp:mock`。
- `approver`：包含 analyst 权限，并增加 `approval:read`、`approval:decide`。
- `admin`：`*`。

默认用户获得 `analyst`；配置中的 Mock approver user id 获得 `approver`。这只是联调身份源，生产替换为 Casbin、OPA 或企业 IAM。角色不能由 LLM、Prompt 或请求体直接声明。

## API

### 创建消息

`POST /api/sessions/{session_id}/messages`

需要 `agent:invoke`。若发生中断，返回 `approval_status=pending`、`run_id` 和 `approval_ids`，Run 状态存为 `interrupted`。

### 查询审批

`GET /api/approvals/pending?tenant_id=...&actor_id=...`

需要 `approval:read`，只返回当前租户数据。

### 审批决策

`POST /api/approvals/{approval_id}/decision`

请求包含 `tenant_id`、`actor_id`、`decision=approved|rejected`。需要 `approval:decide`。批准后恢复 Graph；拒绝后 Graph 走拒绝响应，不执行工具。

## 错误处理

- 无权限：`403`。
- 审批不存在或跨租户访问：`404`，避免泄露资源存在性。
- 已被相反决策处理：`409`。
- checkpoint 不存在或不可恢复：`409`。
- 调用指纹不一致：Graph 返回 `approval_payload_mismatch`，工具不得执行。

## 非目标

- 不实现企业 SSO、JWT 验签或组织架构同步。
- 不实现多人会签、审批委托、超时升级和撤回。
- 不把 checkpoint 数据与业务审批表合并。
- 不在本轮迁移到 Postgres Saver；本地继续使用官方 SQLite Saver。

## 测试

- 指纹稳定性和参数篡改检测。
- Tool Gateway permission scope 与审批哈希校验。
- Approval Repository 幂等创建和状态迁移。
- Graph 首次运行中断、批准恢复、拒绝恢复。
- RBAC 无权限、跨租户和 approver 成功路径。
- Session/Approval API 全链路与 Run 状态更新。
