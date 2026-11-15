# MCP 权限中断与阻塞恢复实施计划

> **执行要求：** 使用 `superpowers:subagent-driven-development` 在本会话按任务实施，遵循 TDD；规格与质量评审后进行最终验证。用户已确认方案并要求实施，不再重复询问同一设计选择。

**目标：** 权限失败且本次候选包含 web_search 时才明确询问联网替代，并解除已核实只读历史调用对当前会话的误阻塞。

**架构：** MCP 边界负责安全分类，Tool Agent 的薄适配层负责人工输入与恢复；复用通用 user_input_gate 和既有搜索网关，不把权限选择混入审批授权。

**技术栈：** Python、LangGraph、Pydantic、SQLAlchemy、PostgreSQL、pytest。

## 任务一：权限分类及人工选择（实施代理）

**文件：** `backend/app/tools/mcp_http.py`、必要的独立权限分类模块、`backend/app/graph/nodes/tool_agent.py`、必要的独立恢复模块、`backend/app/graph/nodes/skill_step_check.py`/Skill 工作流契约（仅受控替代确实需要时）、对应 unit/integration 测试、`docs/user-input-interrupt.md`。

- [x] 先增加失败测试：用真实 `ToolMessage(status="error")` 表达 TuShare 明确权限拒绝；断言 `provider_permission_denied`，安全消息中不含原始 secret。通用远端错误仍为 `mcp_tool_execution_failed`。
- [x] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_mcp_http_tool.py -q`，确认新增测试因缺少权限识别而失败。
- [x] 在适配边界识别明确的权限拒绝；保留未知错误的保守行为。只读资格必须由可信本地约束确认，不信任模型声明。
- [x] 增加 Tool Agent 测试：失败后保存失败 ToolCallRecord/候选上下文，scheduler 串行投影 `pending_user_input`，尚无终结 TaskResult；稳定 source 定位事件。
- [x] 增加恢复测试：同意后只能绑定 web_search，原工具调用次数仍为一；拒绝后不调用任何工具且输出缺失说明；非法/过期响应不生效；搜索权限和预算仍有效。
- [x] 增加候选边界测试：固定 Skill 唯一候选不含 web_search，不额外兜底；全局 Registry 有搜索、本次候选无搜索，也不提供联网选项。非权限失败仍遵守原停止规则。
- [x] 实现薄适配层，复用 `pending_user_input`、`user_input_response` 和原 scheduler/gate，不在远端工具执行函数内部直接 interrupt。
- [x] 增加真实 LangGraph checkpoint/resume 集成测试：调用失败、中断、恢复选择、结束；证明暂停/恢复不重复工具执行。
- [x] 验证 durable 回执：只读权限拒绝可结算，未知写入失败仍 unknown。不得通过普遍降低风险级别实现。
- [x] 更新中文交互说明，列出联网不是原 MCP 权限授权、权限未知策略及固定 Skill 替代语义。

## 任务二：定向修复历史回执（主代理运维步骤）

- [x] 通过只读 SELECT 再次确认规格中唯一 receipt、其 session/run/tool、原摘要及终态。
- [x] 用单条精确条件 UPDATE/行锁事务把 unknown 结算为 settled，JSON 摘要保留原失败并追加 reconciliation（用户确认权限不足、只读查询、时间、原状态）。更新行数必须恰为一，否则 rollback。
- [x] 在新只读连接中查询该回执和全会话 blocker，验证 `ExecutionControl.ensure_tool_effects_resolved(risk_level="medium"/"high")` 不再抛出该误拦截；不执行外部工具。
- [x] 将不含凭证的修复证据写入中文运维记录，说明不回填成功、不重放旧用户问题。

## 任务三：审查与交付

- [x] 独立规格评审通过后进行质量评审；修复所有实质问题。
- [x] 执行新增测试、MCP/Tool Agent/Skill/interrupt/durable 相关回归、完整后端测试、ruff 和 `git diff --check`。
- [x] 确认 README、VSCode 和已有 trace 改动保持原样；不自动启动调试端口，不创建 PR 或推送。
- [x] 报告新增交互、历史回执修复结果、实际验证命令结果与服务实际启用了自动重载的事实。

## 执行与验证记录

- 实施代理完成首轮权限分类和交互后遇到额度限制，由主代理接手并完成剩余实施；未采用其未完成状态作为通过依据。
- 首轮 MCP 分类测试：4 failed / 19 passed → 24 passed。交互首轮：11 failed / 1 passed → 12 passed。
- 主代理补充测试复现并修复：并发中断单值通道冲突、拒绝后继续提示、搜索实体丢失、长结果来源链接丢失、权限限制误报预算耗尽。
- 独立规格评审发现恢复入口忽略当前 Principal scopes；入口级测试先复现旧权限残留，随后用 `Command.update` 修复，空列表撤权也生效。规格复审 PASS，随后独立质量评审 PASS；两项评审均静态只读，未冒充独立全量执行。
- 最终新增/关联测试：77 passed；原工具、Skill、审批及调度链路：134 passed；前端通用选择 Hook：25 passed。
- 最终全后端：`.venv/bin/python -m pytest backend/app/tests -q` → **1530 passed, 50 skipped, 2 warnings，181.85 秒**。两项警告为既有 pytest 插件重复导入、JWT 测试短密钥警告。
- Ruff 对本次文件检查通过，仅对 `mcp_http.py` 两处既有外部错误边界 `BLE001` 作按文件忽略；已与 HEAD 对比确认并非新增。`git diff --check` 通过。
- 最后只读 PostgreSQL 核验：目标回执 settled、原结果 failed、reconciliation 保留、当前 Session blockers=0。
- 8000 上既有 Uvicorn 以 `--reload` 运行，未由本任务手动重启；自动重载后 `/health` 返回 `status=ok`。5678 调试端口未监听。不调用真实模型、联网搜索或 MCP 做付费验收。
- 代码保留在功能分支，不自动合并 dev、不推送。既有 README、trace 文件 SHA-1 与实施前相同，VSCode 文件未操作。
