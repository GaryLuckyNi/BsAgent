# DeepSeek Search API 迁移实施计划

> 执行方式：使用 superpowers:executing-plans 在当前聊天内逐项执行；用户已明确授权设计后直接实施，无需再次征求方案许可。遵循项目约定，在 `codex/deepseek-search-api` 分支工作，不创建 worktree。

**目标：** 默认直接请求 DeepSeek Search API，保留摘要证据与既有执行治理，Fetch 留待第二阶段。

**架构：** `WebSearchTool` 依赖公共客户端协议，工厂选择直连或显式旧模式。本地执行绑定冻结实现和非秘密配置；证据继续使用现有来源结构。

**技术栈：** Python 3.11、httpx、Pydantic、pytest、Langfuse。

## 执行控制与暂停恢复

- 用户要求全自动推进；系统权限审批仍须遵守。
- 用户要求下一次上下文压缩开始即暂停。若后续通过压缩摘要恢复且时间早于北京时间 2026-10-26 00:25，先保存当前进度，停止业务修改及验证，等待已安排的继续任务。不要把压缩当作继续执行授权。
- 已创建当前聊天 heartbeat：`search-api`，北京时间 00:25 继续。若此前已完成，继续任务只检查完成状态，不重复修改；完成后停用自动化。
- 进度以本文件、Git diff、提交和验收记录为准。不得声称未运行的真实接口或数据库检查已通过。

## 任务一：独立客户端与公共契约

文件：新增 `backend/app/search/base.py`、`backend/app/search/deepseek_api.py`、`backend/app/tests/unit/test_deepseek_search_api.py`；修改 `search/deepseek.py`、`search/schemas.py`、`search/__init__.py`、`tools/web_search.py`。

- [x] 编写基于 `httpx.MockTransport` 的失败测试，覆盖实际路径、Bearer 请求头、`count`、`need_content=false`、摘要优先、未知覆盖、空结果及异常分类。

```python
assert request.url.path == "/v1/tools/search"
assert json.loads(request.content)["need_content"] is False
assert result.answer == ""
assert result.coverage.status == "unknown"
```

- [x] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_deepseek_search_api.py -q`，确认缺少新实现导致失败。
- [x] 提取相同异常类型的公共定义并保留旧导入兼容，新增客户端，使用一次 HTTP 请求，校验响应并归一化来源；不新增 SDK 或内部模型调用。
- [x] 原始问题仅通过可信可选上下文绑定，长度合法才传递；查询超长拒绝且没有远端请求。
- [x] 重跑新客户端、旧客户端和搜索工具测试，保留红绿测试结果。

## 任务二：配置、工具版本与执行绑定

文件：修改 `backend/app/core/config.py`、`backend/app/search/factory.py`、`backend/app/tools/default_registry.py`、`backend/app/tools/web_search.py`、`backend/app/tools/execution_gateway.py`、配置与默认目录单测。

- [x] 先加入失败测试：默认模式为 `deepseek_search_api`，独立地址不跟随模型地址，新旧配置或超时变化改变绑定身份。

```python
assert settings.web_search_provider == "deepseek_search_api"
assert entry.execution_binding.provider == "local"
assert before.execution_binding_digest != after.execution_binding_digest
```

- [x] 执行相应单测，确认断言失败。
- [x] 配置增加新模式和地址；工厂构造对应客户端；显式旧模式不变更协议。注册时提供本地绑定及实际实现身份，不将密钥纳入摘要。
- [x] 工具版本更新到 `2.0.0`，查询约束更新到 128；工具超时随实际配置冻结，固定错误消息覆盖认证、余额与限流。
- [x] 重跑配置、默认目录和搜索工具测试。

## 任务三：上下文与恢复证据

文件：修改 `backend/app/graph/nodes/tool_agent.py`、`backend/app/graph/nodes/permission_fallback.py`、`backend/app/graph/recovery/executor.py`、必要的证据投影及关联测试。

- [x] 先编写失败测试，令搜索 `answer=""`，仅在来源中提供摘要与日期；确认恢复观测及集中来源表仍保留它们。

```python
assert observation["search_result"]["results"][0]["snippet"] == "PE 为 10"
assert observation["coverage"]["status"] == "unknown"
```

- [x] 核对普通、旧权限回退和批次恢复的可信上下文构建位置，加入短原始问题的绑定，不将任务目标冒充原始问题。
- [x] 恢复输出按既有 8,000 字符预算保留摘要、日期和截断标记；不修改授权快照或原始回执。
- [x] 恢复查询不再静默截断到 500；超出新输入契约时明确失败、无远端调用，不自动重写冻结目标。
- [x] 运行恢复、来源投影、工具输入及图级权限恢复回归。

## 任务四：观测、文档与验收

文件：新增直连观测单测、真实接口探针和 `docs/acceptance/2026-10-25-deepseek-search-api-migration.md`；修改 `.env.example`、README 及设计状态。

- [x] 先添加观测断言：一个实际 Search 请求对应一个 API span、零搜索 Generation，`metadata_only` 不带查询正文，凭证不泄漏。
- [x] 运行观测测试，确认新行为失败后补充实现。
- [x] 更新配置说明、摘要语义、手动回滚及历史回执限制。
- [x] 运行相关回归，随后运行后端测试与修改文件的 Ruff；记录已有环境限制和非本次问题。
- [x] 凭证和网络可用时对实体核实、新闻、估值资料做真实 Search 探针，记录真实来源、耗时和调用数，不输出密钥。完整研究链路依赖可用时再执行，不将 API 探针等同于端到端验收。
- [x] 自审 diff，修复本轮问题，提交代码与中文验收记录；不自动合并或部署。

## 当前进度

2026-10-26：第一阶段已完成。后端最终全量 2015 passed、53 skipped、2 个既有警告；Ruff 与 diff 检查通过。三条真实直连及旧模式对比成功，隔离的真实 Tool Agent → Gateway → Search → 合成节点成功。独立评审问题已复现、修复并复核通过。完整生产会话、真实权限恢复和 Fetch 未执行；详细边界见 docs/acceptance/2026-10-25-deepseek-search-api-migration.md。保留功能分支，不自动推送或部署。
