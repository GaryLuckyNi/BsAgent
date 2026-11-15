# Run 级工具目录一致性实施计划

> 使用 writing-plans 与 executing-plans 在当前工具功能分支实施，用户已批准范围并明确禁止节点级刷新。不创建 worktree，不提交此前其他工作。

**目标：** 固定 run 的目录及索引，把不可用或过期调用作为工具错误交给模型。

**方案：** 在 ToolCatalogRuntime 保存 run 的配套快照引用，初始执行与恢复共享；Gateway 在调用前检查最新可用目录，但执行仍使用捕获条目。Graph 节点不动态换目录。

**技术栈：** Python、LangGraph、ToolCatalogRuntime、ToolExecutionGateway、pytest。

## 1. 回归测试

- [x] 新增 tests/unit/test_catalog_refresh.py，覆盖 run 快照固定、新 run 更新、Gateway 派发前移除/改契约/无关变更，以及在途结果保存。
- [x] 将既有草案目录错误测试从抛异常改为业务失败结果；先运行确认失败。

## 2. 固定快照与调用检查

- [x] catalog_runtime.py 提供 run_key 绑定、读取和释放，以及显式刷新触发；注册表与索引原子发布。
- [x] execution_gateway.py 接收可选当前目录读取器，只在执行前比较目标工具条目，失败标记 not_started。
- [x] tool_agent.py、permission_fallback.py、recovery/executor.py 将读取器传给 Gateway；工具目录错误以业务失败返回，保留现有门禁。

## 3. Run 初始化及恢复

- [x] runtime.py 在 run 初始化绑定目录，在普通输入与审批恢复时取回同一绑定；所有节点仍共用固定快照，完成后释放。
- [x] 恢复前继续验证会话归属和确认身份。旧绑定不可取时，旧待执行工作产生工具错误，由模型收尾，禁止隐式换契约执行。
- [x] 持久化 API 测试验证恢复后正常结束，下一问不被 session_busy 拦截；保留临时失败重试测试与前端终态清理。

补充覆盖：建图失败、生产者失败、暂停后取消、回执延迟落库取消与失联执行恢复到终态均释放快照；有效中断和未决取消保留绑定。过期恢复分支仍校验原选项与审批决定。

## 4. 验证

- [x] 运行目录、Gateway、Tool Agent、恢复门禁及 Graph/持久化输入审批集成测试。
- [x] 运行前端会话测试、Ruff、diff 检查并记录结果和跨进程恢复限制。

验证结果：后端全量 2095 项通过、53 项跳过；前端会话测试 40 项通过。Ruff 与 diff 检查通过，详见 `docs/acceptance/2026-10-27-catalog-refresh.md`。
