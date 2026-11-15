# Langfuse 调度观测实施计划

> 执行方式：本会话使用 superpowers:executing-plans 顺序实施；用户已批准范围并要求开始。遵循项目约定留在 dev，不创建 worktree，不提交已有工作区改动。

**目标：** 在本地 Langfuse 看懂调度输入、状态更新、下一节点及命中的路由分支。

**架构：** 扩展现有 SDK 导出投影；分发函数保留全部原分支，在实际返回点附带路由原因。独立摘要函数只处理观测副本，故障不影响 Graph。

**技术栈：** Python、LangGraph、Langfuse SDK、pytest、内存 OTel exporter。

## 任务 1：先建立失败测试

- [x] 新增 `backend/app/tests/unit/test_langfuse_scheduler.py`：使用真实 StateGraph 和 SDK 测试首次派发、成功结果触发 Planner、结束和内容模式；覆盖观测异常隔离。
- [x] 在 `test_langfuse_langchain_content_adapter.py` 测试 scheduler 输入投影、空输出和脱敏。
- [x] 运行新增测试，确认缺少 span/投影导致失败。

## 任务 2：最小实现

- [x] 新增 `backend/app/operations/langfuse_scheduler.py`，实现安全调度摘要和分发观测更新，禁止记录 Send 完整参数。
- [x] 修改 `backend/app/graph/runtime.py`，在分发分支返回点记录真实原因，保持返回值和优先级。
- [x] 修改 `backend/app/operations/langfuse_langchain_content.py`，开放 scheduler 投影和空的状态更新。
- [x] 运行调度及 Langfuse 回归测试。

## 任务 3：文档与验证

- [x] 更新 `docs/langfuse-local-observability.md` 的输入输出表和阅读方法，解释成功工具调用后仍需评审的当前策略。
- [x] 执行相关单测、权限回退集成测试、Ruff 和 `git diff --check`；报告实际结果，不宣称旧 Trace 已补全。

验证命令：

```bash
.venv/bin/python -m pytest backend/app/tests/unit/test_langfuse_scheduler.py backend/app/tests/unit/test_langfuse_langchain_content_adapter.py backend/app/tests/unit/test_langfuse_langchain_content.py backend/app/tests/unit/test_langfuse_observability.py backend/app/tests/unit/test_task_scheduler.py backend/app/tests/integration/test_permission_fallback_graph.py -q
```

## 验证结果

- 第一轮红灯：8 个新增场景因缺少分发 span 或 scheduler 投影失败；修正测试构造后确认失败原因属于功能缺失。
- 新增嵌套 JSON 敏感信息测试先失败，再启用共享的 JSON 清理与整体长度限制后通过。只读评审又复现 SDK 中文二次序列化突破上限：补充对最终导出字符串长度的红灯测试，再改为直接传递安全 JSON 字符串；1000/2500/20000 上限测试及复核均通过。
- 最终相关回归：`112 passed`。
- 最终后端全量：`1590 passed, 50 skipped, 2 warnings`，耗时 189.24 秒。警告为既有 pytest 插件重复导入和 JWT 测试短密钥。
- 本轮触及 Python 文件 Ruff、`git diff --check` 均通过。
- 本机 Langfuse 最终合成回读验收：[Trace feed61cbfe5337f9628772f5d612b278](http://localhost:3000/project/bsagent-content-local/traces/feed61cbfe5337f9628772f5d612b278)，3 个 scheduler、3 个 dispatch span，命中 `ready_tasks`、`new_results_require_review`、`planner_finished`；Generation 为 0。这里只验证观测链路，不代表真实模型或行情分析质量。
- 未修改数据库结构，未回填旧 Trace，未提交或推送；保留原有工作区改动。
