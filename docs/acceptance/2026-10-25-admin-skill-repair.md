# Admin Skill 分类与提示框修复验收

日期：2026-10-25

## 修复范围和原因

- 历史分类无法移除：前端把不在当前目录中的已选项标记为 `disabled`，Element Plus 因此不显示标签移除按钮。现在失效项保留提示、允许移除；移除后不再进入候选列表。父级为空但仍有历史子级值时，仍允许清理子级。
- 三个旧 Skill 无法进入模型上下文：历史 `task_workflow` 意图和独立子域与现有分类不匹配，运行时过滤将其排除。按用户确认，仅修改这三个 Skill 的分类，不绕过过滤。
- Summary tooltip 横跨页面：表格列上的 `tooltip-options` 未生效。改为列支持的 `show-overflow-tooltip` 对象配置，使用专属弹层样式。预览宽度最大 360px（小屏不超出视口），正文最多六行，保留换行，并固定提示“完整流程请点击‘编辑’查看”。原始流程内容不截断、不改写。

## 实际配置修复

目标仅限 `company_snapshot`、`financial_report_analysis`、`risk_scan`：

| 字段 | 修复后取值 |
| --- | --- |
| `supported_intents` | `financial_research` |
| `supported_domains` | `a_share_equity_research` |
| `supported_sub_intents` | `general_research` |

通过本地 Admin 接口执行定向更新，更新前检查旧值及并发变更，更新后检查其他字段未变化（允许 `updated_at` 更新）。名称、版本、启用状态、风险等级和流程正文均保留，其他 Skill 未修改。

备份和证据位于 [修复目录](./2026-10-25-admin-skill-repair/)：`before.json` 为原配置，`after.json` 为更新后配置，`verification.json` 为上下文验证结果，`repair.py` 为本次定向修复脚本。脚本不是自动迁移，已有备份时拒绝重复执行。

后续按用户明确要求，默认 Skill 初始化已禁用，不再自动创建这三个旧 Skill。已有管理员配置保留，不自动删除或覆盖。测试覆盖空库不创建、重复初始化无新增，以及已有配置和禁用状态保持不变；上表描述的是此前已有配置的分类修复，不表示新环境仍会自动生成这些 Skill。

## 验证结果

- Admin 后端：`../../.venv/bin/python -m pytest tests -q --tb=short`，36 项通过，包含禁用默认初始化、重复执行与已有管理员配置保护测试。
- Admin 前端：复用主前端已安装的 Vitest，运行 `../../frontend/node_modules/.bin/vitest run tests/skills-view.test.mjs --globals --reporter=dot`，2 项通过；验证真实 Element Plus 标签可移除并保存，以及实际表格 tooltip 使用专属样式类和全文入口。
- Admin 前端构建：`npm run build` 通过，包含 Vue 类型检查和生产构建；保留现有第三方注释及大包警告，不在本次扩大优化范围。
- 实际浏览器：旧分类标签出现移除按钮，移除父级时关联子级同步清理；验证后取消编辑，未保存测试性清空。最终 tooltip 实测宽 360px、高约 147px，六行预览及底部全文提示正常显示。
- 实际运行时加载：使用 `AdminSkillDefinitionProvider`、`SkillRegistry` 和生产能力发现逻辑生成 Planner 输入，三个 Skill 均出现在序列化后的 `skills` 上下文中。未发起付费模型调用。

## 边界

本次恢复的是分类编辑与能力可见性，不代表三个工作流已完成端到端执行验证。其原文仍引用旧的 `mcp.local.stock.*` 工具；这些依赖是否存在、参数是否兼容，需要另行核对，本次未擅自改写流程。没有清空数据库、删除回执或修改权限恢复机制。
