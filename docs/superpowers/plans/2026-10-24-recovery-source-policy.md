# 权限恢复来源约束最小接入实施计划

> 执行方式：用户已选择当前会话自主继续，使用 superpowers:executing-plans；不启动子代理，不创建 worktree。

**目标：** 在原恢复协议内接通真实来源依据，不新建策略平台。

**架构：** 部署配置提供业务许可，Run 冻结配置，原 Intent Router 与 Planner 只标记限制。执行层无远端 I/O 的来源解析交集判断，恢复时重新核验；所有实际搜索仍需既有批次确认。

**技术栈：** Python、Pydantic、LangGraph、pytest；前端协议不变。

## 任务一：来源政策解析与冻结快照

文件：新增 `backend/app/graph/recovery/source_policy.py`、`backend/app/tests/unit/test_recovery_source_policy.py`；修改 `backend/app/core/config.py`、`backend/app/graph/state.py`、`backend/app/graph/nodes/permission_fallback.py`、`backend/app/graph/recovery/validation.py`。

- [x] 先写失败测试：默认 unknown；显式 public_supplement_only + unrestricted + public_information 得到 allowed；请求 original_only、任务 original_data、固定 Skill、配置撤回、缺快照全部拒绝。
- [x] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_recovery_source_policy.py -q`，确认因来源政策功能缺失失败。
- [x] 实现 `configured_source_policy()` 返回版本化部署快照，`resolve_source_policy(state, task)` 返回 `{public_alternative, basis}`；参数只读，不接受模型声明的 allowed。
- [x] 新 Run 初始化冻结 `recovery_source_policy`；选择阶段解析来源；恢复校验比较当前解析结果与冻结选择政策，拒绝不一致。
- [x] 重跑单元测试及原 recovery 单元回归。

## 任务二：复用既有模型调用标记来源限制

文件：`backend/app/intent/schemas.py`、`backend/app/intent/classifiers.py`、`backend/app/graph/planning/contracts.py`、`backend/app/graph/planning/planner.py`、`backend/app/tools/web_search.py`；对应 `test_intent_router.py`、`test_dynamic_planner.py`。

- [x] 先写失败测试：新字段缺失为 unclear；合法枚举保留；allowed 等授权值不被接受；原调用次数仍为一次。
- [x] 在 required_schema 和既有提示词里加入同名枚举，规划与修订共用限制说明，不改变 LLM 调用方式/重试预算。
- [x] 更新搜索 description，明确公开资料补充、原始数据边界与确认要求，不做接口白名单。
- [x] 运行来源政策、意图路由、动态规划和反馈测试。

## 任务三：真实接线联合回归与交付记录

文件：`backend/app/tests/integration/test_runtime_capability_recovery.py`、`.env.example`、`docs/tool-agent.md`、`docs/acceptance/2026-10-24-recovery-source-policy.md`。

- [x] 联合测试默认使用真实 configured_source_policy 和 selection_context，移除默认 allowed 替身；只在针对非法快照的测试中保留故障注入。
- [x] 先验证新联合用例失败，再覆盖混合任务、原始数据/来源限制、恢复时撤销及旧快照不扩权；断言搜索次数和健康任务结果。
- [x] 运行后端全量、前端测试与构建、修改范围 Ruff 和 git diff --check；专用 PostgreSQL 两项并发无跳过通过，扩围全量另发现五项修改前已有失败。
- [x] 记录实际结果和保留边界，不启用生产、不执行真实收费请求；完成当前会话自查，作为代码检查点在当前 feature 分支提交，不合并或宣称扩围全量通过。
