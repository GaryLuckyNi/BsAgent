# 审批恢复与 Mock RBAC 实施计划

> **执行要求：** 按任务逐项执行并使用复选框记录状态；所有行为变更必须先编写失败测试。

**目标：** 使用 LangGraph 原生中断恢复完成工具审批闭环，并通过可替换的 Mock RBAC 提供第一版权限控制。

**架构：** Approval Gate 在工具执行前中断 Graph，Approval Repository 保存与工具调用哈希绑定的业务审批记录，审批 API 使用同一 Run 级 checkpoint thread 恢复。RBAC 通过协议注入，Tool Execution Gateway 在执行前校验最新工具权限和已审批哈希。

**技术栈：** Python 3.11、FastAPI、SQLAlchemy、Alembic、LangGraph、Pydantic v2、pytest

---

### Task 1：工具指纹与 RBAC 契约

**文件：**
- 新建：`backend/app/tools/fingerprints.py`
- 新建：`backend/app/security/rbac.py`
- 修改：`backend/app/core/config.py`
- 修改：`backend/app/tools/execution_gateway.py`
- 测试：`backend/app/tests/unit/test_tool_agent.py`
- 测试：`backend/app/tests/unit/test_rbac.py`

- [ ] 编写指纹稳定、参数变化、工具 scope 拒绝和 approved hash 放行的失败测试。
- [ ] 实现规范化 SHA-256 指纹与 `RbacProvider/MockRbacProvider`。
- [ ] 为 `ToolExecutionContext` 增加 permission scopes 和 approved hashes。
- [ ] 在 Tool Gateway 中强制执行 scope 与审批哈希。

### Task 2：审批持久化

**文件：**
- 修改：`backend/app/storage/models.py`
- 新建：`backend/app/approvals/repository.py`
- 新建：`alembic/versions/20260713_0004_approval_records.py`
- 测试：`backend/app/tests/unit/test_approval_repository.py`

- [ ] 编写审批幂等创建、租户过滤、批准和冲突决策失败测试。
- [ ] 新增 `ApprovalRecord` 和 Alembic migration。
- [ ] 实现仓储的 pending 创建、查询和原子状态迁移。

### Task 3：LangGraph Approval Gate

**文件：**
- 新建：`backend/app/graph/nodes/approval_gate.py`
- 修改：`backend/app/graph/state.py`
- 修改：`backend/app/graph/runtime.py`
- 测试：`backend/app/tests/integration/test_graph_runtime.py`

- [ ] 编写高风险工具首次中断、批准恢复、拒绝恢复和参数 hash 不一致测试。
- [ ] 新增 Approval Gate 和批准/拒绝条件路由。
- [ ] checkpoint `thread_id` 改为 `run_id`。
- [ ] 新增 `resume_question_answer_graph`，使用 `Command(resume=...)`。

### Task 4：Session 与 Approval API

**文件：**
- 修改：`backend/app/storage/repositories.py`
- 修改：`backend/app/api/sessions.py`
- 修改：`backend/app/api/approvals.py`
- 修改：`backend/app/main.py`
- 测试：`backend/app/tests/integration/test_session_api.py`

- [ ] 编写消息中断持久化、无权限、待审批查询、批准恢复和拒绝恢复失败测试。
- [ ] 让 AgentRun 支持 `interrupted -> completed|rejected` 更新。
- [ ] 将 interrupt payload 持久化为审批记录。
- [ ] 实现 pending 查询和 decision API，并在恢复前重新解析权限。
- [ ] 流式接口在中断时发送 `approval_required` 事件。

### Task 5：文档与验证

**文件：**
- 修改：`docs/checkpoint.md`
- 修改：`docs/tool-agent.md`

- [ ] 更新 Run 级 checkpoint、审批职责和 Mock RBAC 限制。
- [ ] 运行审批、Tool、Graph 和 Session 定向测试。
- [ ] 运行完整后端测试。
- [ ] 运行 Ruff 与 `git diff --check`。
- [ ] 提交实现。

