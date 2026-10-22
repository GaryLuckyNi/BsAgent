# 本地功能分支集成计划

> 执行方式：使用 superpowers:subagent-driven-development 的独立评审方式，主代理顺序合并，评审代理只读检查交叉边界。按项目约定在当前工作目录处理，不创建 worktree。

**目标：** 将用户指定的三个功能分支合入本地 dev，保留现有会话续传、协作停止和历史上限 0；不合入冲突备份，不推送远程。

**架构：** 顺序集成 Langfuse 基础、延迟优化和上下文压缩。数据库保留两条已有迁移，通过新的汇合 revision 收敛为单一 head；后台生产者继续拥有执行生命周期，压缩快照与 Langfuse 跟踪接入该生命周期。

**技术栈：** Git、FastAPI、SQLAlchemy、Alembic、PostgreSQL、LangGraph、Langfuse、pytest、Vitest。

## 一、保护与预检

- [x] 检查三个分支的差异及共同祖先，确认第四个备份仍包含冲突标记，不合并。
- [ ] 创建合并前恢复引用；仅暂存 README 的原有调试说明，保留 .env.local、langfuse/.env、虚拟环境与原有 stash。
- [ ] 确认无执行中任务后停止本项目自动重载服务，保留 PostgreSQL 数据。

## 二、顺序合并

- [ ] 执行 `git merge --no-ff --no-commit codex/feature-langfuse-cloud-poc`，整合 main、工具网关与启动脚本；运行 Langfuse、durable 与生命周期测试后提交。
- [ ] 执行 `git merge --no-ff --no-commit codex/feature-langfuse-latency`，保留上游 MCP 鉴权、任务防重复与延迟优化；运行 planner、LLM、MCP、durable 测试后提交。
- [ ] 执行 `git merge --no-ff --no-commit codex/feature-context-compaction`，保留 user-input interrupt、后台快照、停止交接与压缩元数据，保持 `CONVERSATION_HISTORY_LIMIT=0`，压缩默认关闭。
- [ ] 为 `20261010_0014` 和 `20261021_0014` 新增无破坏性汇合迁移，运行 `alembic heads` 确认仅一个 head。

## 三、兼容性与验证

- [ ] 重点检查 `backend/app/api/sessions.py`、`backend/app/api/approvals.py`、`backend/app/conversations/dispatcher.py`：压缩字段必须随持久化快照传入后台执行，不能让刷新重新运行模型或停止丢失上下文。
- [ ] 检查 `backend/app/main.py`、`backend/app/worker/celery_app.py`、`compose.production.yml`：保留 turn-worker、任务补落库与恢复维护，同时保留 Langfuse 和压缩任务。
- [ ] 对实际冲突引入的兼容问题先补回归并确认失败，再最小修复；不顺带重构。
- [ ] 使用随机 PostgreSQL schema 验证现有 durable 迁移、上下文压缩迁移与汇合 head；不在未验证时更新实际库。
- [ ] 运行 `.venv/bin/python -m pytest -q`、`npm test -- --run --maxWorkers=1`、`npm run build` 及 `node --test scripts/*.test.mjs`。
- [ ] 独立评审续传/取消、快照/压缩、Langfuse 生命周期与凭据边界，确认第四个备份不是 dev 的祖先。

## 四、交付

- [ ] 恢复 README 原有调试说明，核对本地凭据未进入提交。
- [ ] 验证通过后提交集成结果，按原有配置恢复本地服务；如新迁移会改动现有库，先确认本地目标和迁移行为。
- [ ] 报告三个分支的合并提交、验证结果及明确未合并的备份，不推送、不删除用户原有备份或 stash。
