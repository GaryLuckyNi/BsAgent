# Function calling 参数覆盖优化计划

> 按用户已授权范围使用 writing-plans、executing-plans 与测试先行流程在当前分支实施，不新建 worktree，不提交其他未完成工作。

**目标：** 在同次 function calling 内保持任务需求与工具参数一致，避免主题化缩写遗漏必要指标。

**方案：** 简短通用提示词同时约束目标覆盖、已有证据复用、schema 适配与时间范围；单调用协议保持原样，不添加执行层验收。

## 1. 记录基线

- [x] 读取原 trace，保存任务、schema、实际漏项参数。
- [x] 编写真实原生 function calling 探针，冻结原时间上下文，准备多指标、部分已有证据、专业 schema、无需工具等边界。
- [x] 运行旧提示词探针，记录实际参数与覆盖失败。

## 2. 提示词修改

- [x] 修改 `backend/app/graph/nodes/tool_agent.py`，指导参数覆盖当前任务剩余的关键事实。
- [x] 修改 `backend/app/tools/native_calls.py`，明确单调用数量限制不等于单一信息点。
- [x] 用同一探针比较修改后输出，检查 schema、参数覆盖与无须调用边界。

## 3. 回归

- [x] 运行 Tool Agent、native calling、固定 Skill、来源边界相关测试及必要全量回归。
- [x] Ruff、diff 检查，记录真实参数验证结果与其无法保证检索结果完整的限制。

结果：北向和主力资金从原样本 0/3 覆盖改善到 3/3；后端全量 2118 通过、53 跳过。严格参数覆盖仍为 5/8，原样本三次均缺显式领跌概念，未宣称完整修复。详情见 `docs/acceptance/2026-10-28-tool-call-coverage.md`。
