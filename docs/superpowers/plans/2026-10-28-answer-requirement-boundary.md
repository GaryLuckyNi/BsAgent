# 回答要求边界实施计划

> 使用 superpowers:executing-plans、test-driven-development 和 verification-before-completion，在当前工作区完成，保留其他未提交改动。

**目标：** 恢复研究耗时，消除把内部任务缺口直接当成用户回答缺口的传播。

**架构：** 保留既有 router → planner → synthesis 数据流；统一提示词要求来源，并修正确定性兜底的公开输出。无新模型调用与 schema。

## 步骤

- [x] 修改已有组件测试，让 mock 提供 duration，确认原标题没有耗时后失败，再让标题消费真实 duration。
- [x] 新增合成输入测试覆盖新旧流程原问与扩展 goal 不同，确认原问始终可见，不删除实际取证材料及限制。
- [x] 新增确定性兜底测试，确认旧实现复述内部缺口，再改为部分材料说明，保留数值校验与不完整标记。
- [x] 统一 classifiers.py、planner.py、synthesizer.py 与 synthesis_decision.py 中用户要求、任务标准、辅助核验和关键缺口的关系，避免矛盾提示；必要计算输入仍为必要。
- [x] 失败、恢复、预算的自动限制文案保持执行观察含义；同步既有测试，不更改真实执行状态或授权。
- [x] 后端代码回归 2164 通过、53 跳过；前端 96 通过、构建通过，独立审查已通过。
- [x] 根据用户进一步明确的固定 Skill/开放式边界修正提示，不以用户点名字段作前提，定向 191 通过，包含 Skill 工作流与图集成。
- [x] 真实 trace 固定输入回放：开放式回答不再附加辅助数据缺失，必要输入缺失的计算对照仍拒绝编造结果；记录于 docs/acceptance/2026-10-28-answer-requirement-replay.json。
