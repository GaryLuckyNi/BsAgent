# Generation 延迟修复实施计划

> 执行方式：在当前会话按任务实施，使用 TDD 和 verification-before-completion；按项目约定直接使用 dev，不创建 worktree。用户已批准开始修改诊断报告中的问题。

**目标：** 减少已被真实 trace 证实的无效模型重试，让工具参数生成获得明确的单次调用契约与当前日期。

**设计：** 保留原有 native tool calling、auto 不调用工具、单工具执行校验及有限修复机制。调用模型前增加明确的零或一个调用约束，DeepSeek 工具路径使用 temperature=0。工具节点显式传递北京时间及日期规则，尊重用户指定的历史期间。planner 在解析修订计划前，仅将已成功且不在新活动计划内的历史依赖移动到 evidence_task_ids；失败、未知和活动任务依赖仍严格校验。只排查 MCP 拒绝原因，不将默认 medium 工具批量降级，也不清除未决写入回执。

**技术栈：** Python、LangChain、LangGraph、Pydantic、pytest、DeepSeek。

**实施结果：** 已完成下列任务，最终 329 项相关测试通过，修改文件 lint 与差异检查通过。6 次实际代码重放均首次返回单工具调用；真实搜索隔离流程为 6 次 Generation、24.221 秒，无契约重试。详细证据和原会话未决 MCP 回执边界见 `docs/acceptance/2026-10-22-generation-latency-diagnosis.md`。

## 任务一：模型工具调用契约

涉及 `backend/app/tools/native_calls.py`、`backend/app/llm/deepseek.py`、`backend/app/tests/unit/test_native_tool_messages.py`、`backend/app/tests/unit/test_deepseek_client.py`。

- [x] 先添加失败测试：首次请求即包含最多一次调用约束；auto 允许不调用、required 要求恰好一次；保留未知工具、多调用拒绝和有限重试；不修改调用者 messages。
- [x] 添加 DeepSeek 绑定参数测试：工具调用关闭 thinking、temperature=0。
- [x] 运行新增测试，确认因契约缺失而失败。
- [x] 在模型调用边界添加独立 system instruction，明确不可一次调用多个工具，也不可重复调用同一工具；调用时只输出调用、不额外叙述。不调用的普通分析仍正常返回。
- [x] 修改 DeepSeek 绑定参数并运行该组回归。

## 任务二：日期锚定

涉及 `backend/app/graph/nodes/tool_agent.py`、`backend/app/tests/unit/test_native_tool_messages.py`、`backend/app/graph/planning/planner.py`。

- [x] 添加失败测试：工具普通任务与固定 Skill 都收到带时区的 current_datetime；当前查询与用户指定历史期间的规则明确。
- [x] 工具 instruction 传递 current_datetime/timezone，在 system prompt 要求当前查询使用锚定时间；明确历史研究不能被替换成今天。
- [x] planner review 同样传递 current_datetime/timezone，避免修订任务漂移。
- [x] 运行工具消息与 planner 回归。

## 任务三：成功历史依赖确定性转换

涉及 `backend/app/graph/planning/planner.py`、`backend/app/tests/unit/test_planner_feedback.py`。

- [x] 添加失败测试：复现已删除政策任务仍在 depends_on 中的响应，要求一次模型调用成功，成功历史任务转为 evidence_task_ids，活动依赖保持原样。
- [x] 覆盖失败历史结果、未知依赖、循环和自依赖仍被拒绝；证据去重，原始返回对象不被改写。
- [x] 在 PlannerFeedback.model_validate 之前复制并转换上述明确可判定的历史依赖；所有原有 schema、权限、重复研究和证据成功性校验继续执行。
- [x] 增强反馈提示词，说明活动执行依赖与历史证据的区别，运行 planner 回归。

## 任务四：真实验证与交付

- [x] 读取 MCP 风险定义及未决副作用保护，确认不通过放宽保护来缩短调用链。
- [x] 运行相关单元测试、图集成回归、ruff 和 git diff --check。
- [x] 按原 trace 重建真实工具请求验证首次有效率，并在独立会话/隔离测试运行原始业务问题，避免旧会话未决副作用污染测试；不执行写入型工具。
- [x] 更新诊断报告，区分实测结果、样本局限及未修复项；不承诺单条 trace 不能证明的稳定提速比例。
