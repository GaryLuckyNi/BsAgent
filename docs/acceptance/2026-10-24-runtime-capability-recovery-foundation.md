# 运行时能力恢复：第一检查点

日期：2026-10-24（Asia/Shanghai）。分支：`codex/runtime-capability-recovery`。

## 状态与范围

本次只完成实施计划任务一至四的基础模块，不是整套恢复链路完成或上线记录。

- `ToolCallResult.execution_state` 区分 not_started / completed / uncertain；旧失败保守为 uncertain，旧成功兼容为 completed。归一化不会把带部分结果的矛盾拒绝变成可安全重试。
- MCP 在丢弃原文前按错误协议分类；权限、认证、限流、额度、参数错误分开。只在明确错误上下文、无正常/部分结果且有受信前置拒绝语义时证明未执行。未知错误信封不会被当成调用成功。
- 搜索持久化入口在 Run 锁内预留身份与预算；已结算完整结果复用，running/unknown、绑定变化、身份冲突和不完整摘要不重发。数据库短故障只重试结果落库，不重试外部搜索，也不把截断摘要重新标为完整。
- 恢复选择以同 Run 协议事件保存；同批同选择幂等，不同选择或摘要冲突不能重新打开已消费授权。
- 本轮能力观察默认只命中相同参数指纹。租户、用户、request、工具版本、绑定或可得账号修订不同都不能复用；缺省工具版本与同一已解析版本不能绕过限制。
- 候选阶段没有具体参数，不剔除 arguments 级观察；只有明确工具级证据才剔除整条候选。网关在权限/参数校验后、外部派发前再次读取已结算独立回执；跳过仅返回 availability_skip 引用，不新增远端回执。
- 派生观察投影失败不会改变已结算回执；目录、工具风险和原审批参数不被改写。新增路径不调用模型。

## 前置拒绝语义的配置边界

`McpServerConfig.permission_denial_is_pre_execution` 默认 False。它声明受信数据源错误协议的前置拒绝语义，不是工具只读性，更不是账号已有接口权限。

Admin 中明确声明为 TuShare 的受信数据源连接使用其拒绝协议；其他通用 MCP 默认不作此保证。通用部署只有在验证协议后才可设置 `BS_AGENT_MCP_PERMISSION_DENIAL_IS_PRE_EXECUTION=true`，不能为了消除 unknown 而打开。配置变更参与 execution binding 摘要，旧等待可能被目录一致性保护拒绝恢复，应重新发起，不绕过校验。

没有逐 API 的权限或只读白名单，没有调用 tools/list 来猜账号权限，没有降低 risk_level，也没有额外的权限探测请求。

## 验证方式与已知门槛

先建立失败测试，再实现并复测：执行确定性、错误文案和包装、未知服务、混合结果、数字错误码、独立回执防重、预算、重复决定、身份冲突、落库重试、结果截断及错误范围提升等均有受控用例。

PostgreSQL 并发测试位于 `backend/app/tests/integration/test_search_invocation_concurrency.py`，仅使用 `BS_AGENT_TEST_POSTGRES_URL` 指定数据库中的随机隔离 schema。未配置地址时明确跳过，SQLite 通过不能替代数据库行锁并发验收。本次不能宣称这一上线门槛通过。

最新完整后端回归：`.venv/bin/python -m pytest backend/app/tests -q --tb=short`，1705 passed、52 skipped、2 warnings，181.11秒。相对原基线净新增85个通过用例及2个跳过的 PostgreSQL 并发用例。两条警告分别为既有 pytest 模块重写警告和 JWT 测试短密钥警告。

本批变更的相关 Python 文件 Ruff 检查和 `git diff --check` 通过。前端没有代码变更，本次未把上一轮前端通过结果冒充为新批次验收。

## 尚未完成

1. Scheduler 派发批次封存与一次合并 interrupt。
2. 服务端批次摘要/显示版本校验，前端完整展示全部授权条目。
3. 独立恢复执行器及其持久化预算、防重入口接线。目前旧搜索恢复链路没有自动升级到新协议。
4. 覆盖/来源限制与计算依赖的联合验收，以及完整恢复观测。
5. PostgreSQL 并发验证和另行授权的真实数据源验收。

未处理任何历史 unknown，未重放 MCP，未调用真实或付费模型。没有数据库结构迁移，没有重启后端/worker；不保证已有运行进程已加载这些文件，不能据此宣称线上权限问题已解决。
