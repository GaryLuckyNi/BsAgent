# Planner Harness 优化实施计划

**目标：** 保留工具检索，模型自主选择工具调用及计划修订，修复 planner 与执行边界的确定性缺陷。

**架构：** 沿用现有 LangGraph。初始规划与批次后的反馈决策复用 planner，scheduler 仅管理可运行任务和观察检查点。工具候选与调用决定分离。

**技术栈：** Python、Pydantic、LangGraph、DeepSeek 工具调用、pytest。

在 dev 分支逐项实施，不创建 worktree、不提交已有未提交修改。使用测试驱动开发，每组先运行新增测试确认失败，再实现并回归。设计依据为同日优化建议书。

## 1. 计划完整性
- [x] 测试必要末端任务保留、修订保留所有字段、未披露 Skill 的校验修复。
- [x] 修改 planning/planner.py、policy.py、nodes/planner_agent.py；不根据拓扑静默删除任务。
- [x] 回归 test_dynamic_planner.py、test_planning_policy.py。

## 2. 反馈决策与版本
- [x] 测试同样执行结果下 continue/revise/finish 的不同合法选择、无进展修复、已完成任务不可改写。
- [x] 在 contracts.py 声明反馈动作和任务验收条件；在 planner.py 实现受校验反馈请求。
- [x] planner_agent.py 接收完整计划、用户约束、观察和预算；runtime.py 在批次结果变化时请求反馈，不按业务错误触发修订。
- [x] state.py 保存策略版本、反馈次数、观察签名、计划历史与模型决策。新版计划保留明确复用的已完成任务，新增任务使用新版本 ID。

## 3. 能力与搜索策略
- [x] 测试工具检索仍发生、专业候选与 web_search 共存、无候选不强制搜索、模型可以不调用。
- [x] 修改 candidate_resolver.py 取消 web 排名截断与 required 绑定，独立检查 web 权限和作用域。
- [x] 修改 tool_agent.py 清理直接生成搜索调用，添加工具使用策略；纯分析任务通过显式执行类型派发。
- [x] 搜索预算在调用前原子预留，避免按任务顺序分配；保留网关校验和审批恢复。

## 4. 回归与文档
- [x] 补充真实 LangGraph 集成测试覆盖多批次继续、修订后复用、模型完成及搜索选择。
- [x] 运行相关 unit/integration 测试；失败按新行为调整过期断言，不隐藏非预期失败。
- [x] 更新 graph-runtime.md、tool-agent.md、CHANGELOG.md，说明兼容性及尚未进行的真实模型评估。
- [x] 检查 diff 和语法，复核权限、审批、调用记录与预算；如新增问题，再补回归测试。

本轮初始针对性回归：332 项通过；Ruff 检查通过。随后在用户启动的本地服务上完成真实 DeepSeek 链路试运行，发现反馈 JSON 两次不合约与 `auto` 一次返回多个工具调用的边界问题，已增加安全停止和一次修复。真实请求已能走完整图并由模型自主修订；明确请求公开网页时，Checkpoint 确认 `web_search` 被实际调用并生成回答。分析利润变化时，所选两个 Tushare MCP 工具均返回 `mcp_tool_execution_failed`；Checkpoint 的安全分类为 `remote_tool_error`，用户已确认第三方接口的实际原因是权限不足。该错误不属于本轮 Harness 回归，不再调查远端原因；由于权限限制，尚未完成该场景的有效财报证据与回答质量验收。
