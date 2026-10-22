# MCP 权限中断与阻塞恢复实施计划

> **执行要求：** 使用 `superpowers:subagent-driven-development` 在本会话按任务实施，遵循 TDD；规格与质量评审后进行最终验证。用户已确认方案并要求实施，不再重复询问同一设计选择。

**目标：** 权限失败明确询问联网替代，并解除已核实只读历史调用对当前会话的误阻塞。

**架构：** MCP 边界负责安全分类，Tool Agent 的薄适配层负责人工输入与恢复；复用通用 user_input_gate 和既有搜索网关，不把权限选择混入审批授权。

**技术栈：** Python、LangGraph、Pydantic、SQLAlchemy、PostgreSQL、pytest。

## 任务一：权限分类及人工选择（实施代理）

**文件：** `backend/app/tools/mcp_http.py`、必要的独立权限分类模块、`backend/app/graph/nodes/tool_agent.py`、必要的独立恢复模块、`backend/app/graph/nodes/skill_step_check.py`/Skill 工作流契约（仅受控替代确实需要时）、对应 unit/integration 测试、`docs/user-input-interrupt.md`。

- [ ] 先增加失败测试：用真实 `ToolMessage(status="error")` 表达 TuShare 明确权限拒绝；断言 `provider_permission_denied`，安全消息中不含原始 secret。通用远端错误仍为 `mcp_tool_execution_failed`。
- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_mcp_http_tool.py -q`，确认新增测试因缺少权限识别而失败。
- [ ] 在适配边界识别明确的权限拒绝；保留未知错误的保守行为。只读资格必须由可信本地约束确认，不信任模型声明。
- [ ] 增加 Tool Agent 测试：失败后只有 `pending_user_input` 与失败 ToolCallRecord，尚无终结 TaskResult；稳定 source 定位事件。
- [ ] 增加恢复测试：同意后只能绑定 web_search，原工具调用次数仍为一；拒绝后不调用任何工具且输出缺失说明；非法/过期响应不生效；搜索权限和预算仍有效。
- [ ] 增加固定 Skill 测试：中断不会提前终止 workflow，替代来源可审计，原失败记录不被覆盖成成功；非权限失败仍遵守原停止规则。
- [ ] 实现薄适配层，复用 `pending_user_input`、`user_input_response` 和原 scheduler/gate，不在远端工具执行函数内部直接 interrupt。
- [ ] 增加真实 LangGraph checkpoint/resume 集成测试：调用失败、中断、恢复选择、结束；证明暂停/恢复不重复工具执行。
- [ ] 验证 durable 回执：只读权限拒绝可结算，未知写入失败仍 unknown。不得通过普遍降低风险级别实现。
- [ ] 更新中文交互说明，列出联网不是原 MCP 权限授权、权限未知策略及固定 Skill 替代语义。

## 任务二：定向修复历史回执（主代理运维步骤）

- [ ] 通过只读 SELECT 再次确认规格中唯一 receipt、其 session/run/tool、原摘要及终态。
- [ ] 用单条精确条件 UPDATE/行锁事务把 unknown 结算为 settled，JSON 摘要保留原失败并追加 reconciliation（用户确认权限不足、只读查询、时间、原状态）。更新行数必须恰为一，否则 rollback。
- [ ] 在新只读连接中查询该回执和全会话 blocker，验证 `ExecutionControl.ensure_tool_effects_resolved(risk_level="medium"/"high")` 不再抛出该误拦截；不执行外部工具。
- [ ] 将不含凭证的修复证据写入中文运维记录，说明不回填成功、不重放旧用户问题。

## 任务三：审查与交付

- [ ] 独立规格评审通过后进行质量评审；修复所有实质问题。
- [ ] 执行新增测试、MCP/Tool Agent/Skill/interrupt/durable 相关回归、完整后端测试、ruff 和 `git diff --check`。
- [ ] 确认 README、VSCode 和已有 trace 改动保持原样；不自动启动调试端口，不创建 PR 或推送。
- [ ] 报告新增交互、历史回执修复结果、实际验证命令结果与仍需重载服务的事实。
