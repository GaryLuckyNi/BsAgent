# 公共 MCP 说明补全实施计划

**目标：** 以 Celery 异步补全公开第三方接口的字段含义，主链路不等待，执行契约不受模型生成内容影响。

**架构：** 原始 MCP 元数据 → 缺口检测 → SQL 持久化任务与缓存 → 独立 Celery 队列 → 官方文档 Search/Fetch → 有出处的模型提取 → 后续目录快照 → 工具选择与合成共享说明。

**技术：** Python、SQLAlchemy/Alembic、Celery、现有 DeepSeek Search API 与模型客户端、Pydantic。

在当前会话内按 executing-plans 实施，不创建额外 worktree，不提交其他历史未提交修改。

- [x] 先写缺口检测、来源隔离、原始输出契约保留、说明只补缺失而不改变执行契约的回归，观察失败。
- [x] 新建 `backend/app/tool_documentation/contracts.py` 与 `metadata.py`：结构化出处、字段说明、递归缺口检测、公开清单摘要、模型披露与合成投影。
- [x] 先写 SQL 去重、租约、失败冷却、重复投递和旧版本隔离测试，再实现 `repository.py`、模型表和 `alembic/versions/20261030_0016_tool_documentation.py`。
- [x] 先写文档来源限制、Search/Fetch、摘录对照、未知语义与提取失败测试，再实现 `enrichment.py`。
- [x] 先写目录接入与 Celery 任务测试，再实现 `service.py`、`tasks.py`、公共连接声明、独立 worker 队列和部署配置。使用 `pytest backend/app/tests/unit/test_tool_documentation*.py -q` 验证。
- [x] 将已补全说明随目录快照传入原生参数生成，并保存本次实际取数对应说明到 State；合成仅读取对应结果的说明，不将文档当作业务取证。补充真正 Graph/工具/合成输入集成测试。
- [x] 运行相关回归、Ruff、迁移升级/降级检查和差异检查，记录已验证范围与未运行的线上操作。

验证范围见 docs/acceptance/2026-10-30-public-mcp-documentation.md。后续已补齐本机 Redis 认证配置并启动文档 worker，真实现金流量表文档任务经 Redis/Celery 完成并保存至 PostgreSQL；后端重启后健康检查通过。
