# 两条 Skill 步骤验收 trace 对比

## 结论

对比 `556bebe1c67246a13cc27ed47871c3f9` 与 `09bf144b7151604dd4b5710ee8018367`，分叉发生在 Skill 编译阶段，前者没有生成可执行工作流，并非执行后调度遗漏步骤验收。

用户原问均为“分析银之杰管理层薪酬和持股”，Planner 均选择 `yzj_reasearch`。两次交给工作流编译器的 Skill 正文不同：

| 项目 | 前者（15:39，北京时间） | 后者（14:56，北京时间） |
|---|---|---|
| 第一步工具引用 | `mcp.tushare. stock_basic`（点号后有空格） | `stock_basic` |
| 第二步工具引用 | `mcp.tushare.stk_rewards` | `stk_rewards` |
| 编译模型输出工具名 | `mcp.tushare.stock_basic` | `stock_basic` |
| 编译结果 | 两次均未通过，`skill_workflow_invalid` | 一次通过，绑定两个完整工具名 |
| Skill 工作流 | 未创建 | 正常运行并完成 |
| step check | 0 次 | 2 次，两个步骤均加入 `checked_steps` |

前者工具名原文校验要求完整工具名在逐字引用的 `source` 中出现。模型输出去掉空格后的合法完整名，而 `source` 保留了原文中的空格，因此被拒绝。第二次编译修复输入明确给出“工具名称必须在该步骤原文明确出现”，但输出仍采用同一形式，最终返回编译失败。

后者顺序为：编译 → `stock_basic` → step check → `stk_rewards` → step check → Skill 结果整理。前者编译失败后执行的 `stk_rewards` 属于另一个普通任务 `yzj_ownership_and_comp_structure`，不能视为 Skill 内部执行后漏验收。

## 确定性回放

使用 trace 中保存的原文和模型输出，离线调用当前 `compile_workflow`，以本地假工具注册两个相同名称的契约，不调用外部 MCP 或模型：

1. 前者原始正文及两次输出：失败，编译调用两次，错误与 trace 一致。
2. 后者原始正文及输出：成功，绑定 `mcp.tushare.stock_basic`、`mcp.tushare.stk_rewards`。
3. 仅在前者本地副本中规范化点号后的空格，并使引用的 `source` 同步对应：成功。
4. 前者原文保持不变，仅将模型输出的第一个工具名改为原文实际出现的短名 `stock_basic`：成功。

第 4 项说明不只是文本格式问题：当前完整名与短名的校验方式不统一，同样有格式差异的 Skill 原文，会因模型选择完整名或短名而产生不同结果。

## 修复边界建议

应修在工具引用解析和 Skill 编译校验：明确工具标识的规范形式，并在保留逐字来源与工具命名空间约束的前提下，统一引用与解析后的工具身份。不能通过放松字段验收、绕过 step check 或任意选择同名工具解决。

本次为 trace 对比与离线回放，未修改线上 Skill 配置、工具数据或调度实现。
