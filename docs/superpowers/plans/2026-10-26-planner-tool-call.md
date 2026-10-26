# Planner 工具调用草案实施计划

> 执行要求：使用 `superpowers:executing-plans` 逐项实施；用户已确认设计与实施，本会话直接执行，不额外申请 worktree 或重复确认。

**目标：** Planner 为参数齐全的独立普通任务附带可选调用草案，校验后省去执行模型调用。

**架构：** 规划卡片与原生调用共用输入契约投影。Tool Agent 在现有候选解析之后选择采用草案或走原模型流程；全部执行门禁继续共用。

**技术栈：** Python、Pydantic、LangGraph、现有 ToolSearchService 与 Gateway。

## 任务一：完整输入契约与旧数据兼容

文件：新增 `backend/app/tools/input_schema.py`，修改 `tools/native_calls.py`、`tools/planning_candidates.py`；测试 `tests/unit/test_planning_candidates.py`。

- [x] 先改写卡片与 Planner 的测试，验证低风险候选完整 schema、注入字段隐藏及大小预算；观察失败。
- [x] 提取现有投影为 `model_input_schema(spec)`，native 调用使用该函数。
- [x] 卡片增加 `input_schema: dict | None = None`，单项 4000、累计 12000 字符，超预算或脱敏改变 schema 时省略完整契约，不截断。
- [x] 验证 native 与 Planner 的契约一致、旧快照仍能加载。

## 任务二：草案契约与规划提示

文件：修改 `graph/planning/contracts.py`、`graph/planning/planner.py`、`graph/planning/policy.py`；新增 `tests/unit/test_planned_tool_call.py`。

- [x] 先测试 `proposed_tool_call={"candidate_ref": ref, "arguments": {"name": "银之杰"}}` 可随规划输出保存；旧计划签名不变。
- [x] 添加严格的 `ProposedToolCall` 模型，仅允许引用与 JSON 参数，默认 null；非法或不适用草案在执行前回退，不为可选优化强制增加 Planner 修复调用。
- [x] 在初始规划和修订的 required_schema 与系统提示加入可选草案规则，删除与其矛盾的“不得生成参数”绝对规则。
- [x] 新字段为空时不参与旧签名；防重工作比较排除草案建议字段，保持真实任务变更检查。

## 任务三：Tool Agent 直接采用与回退

文件：新增 `graph/planning/tool_call_proposal.py`，修改 `graph/nodes/tool_agent.py`；测试新增单元用例及 `tests/integration/test_planning_candidates_graph.py`。

- [x] 用图测试先复现独立任务有草案仍调用 bind_tools 一次，作为优化前基线。
- [x] 新模块返回 `NativeToolSelection | None` 和固定原因，检查普通任务、无依赖、引用、快照、当前候选、low 风险、日期规范化、参数及凭证；不执行工具。
- [x] 复用现有候选解析，成功采用草案时跳过 `_generate_tool_selection`；日期规范化与 `_trusted_request` 继续共用，不绕过 Gateway。
- [x] discovery 记录 `proposal_status`，不记录参数。回退时正常调用原模型，保持原有次数上限。
- [x] 对同输入比较检索一次、实际工具一次、优化路径执行模型零次；验证非法参数、依赖任务、未知引用、无完整契约、目录/权限/风险变化不直接派发。

## 任务四：验证与交付

- [x] 执行 `.venv/bin/python -m pytest backend/app/tests/unit/test_planning_candidates.py backend/app/tests/unit/test_planned_tool_call.py backend/app/tests/integration/test_planning_candidates_graph.py -q --tb=short`，全部通过。
- [x] 执行后端全量 `.venv/bin/python -m pytest backend/app/tests -q --tb=short`，运行修改范围 Ruff、`git diff --check`、自审及独立评审。
- [x] 更新中文 `docs/tool-agent.md` 和验收记录，准确区分受控调用次数收益与尚未测量的线上 token/延迟；提交本地功能分支，不发布、不重放用户 Run。

最终结果：`2056 passed, 53 skipped`，两项基线既有警告；Ruff 与独立复核通过。受控图显示单任务选择模型调用从一次变为零，多个独立任务同样可采用草案；不宣称尚未测量的真实总 token 或延迟收益。完整证据见 `docs/acceptance/2026-10-26-planner-tool-call.md`。
