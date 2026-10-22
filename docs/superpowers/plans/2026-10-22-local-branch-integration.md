# 本地功能分支集成计划

> 执行方式：使用 superpowers:subagent-driven-development 的独立评审方式，主代理顺序合并，评审代理只读检查交叉边界。按项目约定在当前工作目录处理，不创建 worktree。

**目标：** 将用户指定的三个功能分支合入本地 dev，保留现有会话续传、协作停止和历史上限 0；不合入冲突备份，不推送远程。

**架构：** 顺序集成 Langfuse 基础、延迟优化和上下文压缩。数据库保留两条已有迁移，通过新的汇合 revision 收敛为单一 head；后台生产者继续拥有执行生命周期，压缩快照与 Langfuse 跟踪接入该生命周期。

**技术栈：** Git、FastAPI、SQLAlchemy、Alembic、PostgreSQL、LangGraph、Langfuse、pytest、Vitest。

## 一、保护与预检

- [x] 检查三个分支的差异及共同祖先，确认第四个备份仍包含冲突标记，不合并。
- [x] 创建合并前恢复引用；仅暂存 README 的原有调试说明，保留 .env.local、langfuse/.env、虚拟环境与原有 stash。
- [x] 确认无执行中任务后停止本项目自动重载服务，保留 PostgreSQL 数据。

## 二、顺序合并

- [x] 执行 `git merge --no-ff --no-commit codex/feature-langfuse-cloud-poc`，整合 main、工具网关与启动脚本；运行 Langfuse、durable 与生命周期测试后提交。
- [x] 执行 `git merge --no-ff --no-commit codex/feature-langfuse-latency`，保留上游 MCP 鉴权、任务防重复与延迟优化；运行 planner、LLM、MCP、durable 测试后提交。
- [x] 执行 `git merge --no-ff --no-commit codex/feature-context-compaction`，保留 user-input interrupt、后台快照、停止交接与压缩元数据，保持 `CONVERSATION_HISTORY_LIMIT=0`，压缩默认关闭。
- [x] 为 `20261010_0014` 和 `20261021_0014` 新增无破坏性汇合迁移，运行 `alembic heads` 确认仅一个 head。

## 三、兼容性与验证

- [x] 重点检查 `backend/app/api/sessions.py`、`backend/app/api/approvals.py`、`backend/app/conversations/dispatcher.py`：压缩字段必须随持久化快照传入后台执行，不能让刷新重新运行模型或停止丢失上下文。
- [x] 检查 `backend/app/main.py`、`backend/app/worker/celery_app.py`、`compose.production.yml`：保留 turn-worker、任务补落库与恢复维护，同时保留 Langfuse 和压缩任务。
- [x] 对实际冲突引入的兼容问题先补回归并确认失败，再最小修复；不顺带重构。
- [x] 使用随机 PostgreSQL schema 验证现有 durable 迁移、上下文压缩迁移与汇合 head；不在未验证时更新实际库。
- [x] 运行 `.venv/bin/python -m pytest -q`、`npm --prefix frontend test -- --run --maxWorkers=1`、`npm --prefix frontend run build` 及 `node --test scripts/*.test.mjs`。
- [x] 独立评审续传/取消、快照/压缩、Langfuse 生命周期与凭据边界，确认第四个备份不是 dev 的祖先。

## 四、交付

- [ ] 恢复 README 原有调试说明，核对本地凭据未进入提交。
- [ ] 验证通过后提交集成结果，按原有配置恢复本地服务；如新迁移会改动现有库，先确认本地目标和迁移行为。
- [ ] 报告三个分支的合并提交、验证结果及明确未合并的备份，不推送、不删除用户原有备份或 stash。

## 集成决策与验证记录

- Langfuse 保留完整基础观测与同步流延迟统计，补齐 `metadata_only` 正文屏蔽、worker 生命周期与后台评估上下文传播。
- 协议 HTTP 请求仅冻结附件、旧历史和取消交接，并与 durable payload 一次提交；压缩模型在持有执行所有权的后台生产者中运行，进度先持久化再执行摘要。
- 压缩模型调用前后及摘要激活前检查取消；启用压缩时将取消交接计入固定预算，历史上限仍为 0。
- 独立评审发现 Planner 把相同能力检索语句或文本相似误当相同取证目标；补充跨年份、股票和指标回归后，改为仅对确定相同的规范化目标硬拦截，模糊证据覆盖交由反馈模型判断。
- 后端全量：1454 通过、50 跳过（未配置的外部依赖/真实服务测试等）；测试存在原有 pytest 插件和测试 JWT 短密钥警告。
- PostgreSQL 随机隔离 schema：23 通过，覆盖从共同前驱及任一 0014 升至 0015、回退和重升，保留已有消息正文。
- 前端：66 通过；生产构建通过，保留原有 bundle 大小、循环 chunk 与第三方注释警告。
- 开发启动脚本：8 通过；定向语法、导入与未定义符号检查通过。
- 规格验收和代码质量复审均通过；没有推送远程，第四个冲突备份保留原状。
