# Checkpoint 运行时说明

## 职责边界

`persist_agent_run` 负责审计和历史记录，保存会话、run、trace、memory candidate 和已审批经验记忆。它回答“这次 run 最终发生了什么”。

LangGraph checkpointer 负责图执行恢复，保存节点级 channel values、checkpoint 版本、父 checkpoint 和 pending writes。它回答“这个 graph 执行到哪一步，如何从那里继续”。

这两个能力使用同一个 `session_id` 关联，但不共用存储模型。

## 当前后端

`BS_AGENT_GRAPH_CHECKPOINTER_BACKEND` 支持：

- `sqlite`：默认值，使用官方 `langgraph-checkpoint-sqlite` 包，适合本地持久化和单机部署验证。
- `memory`：使用 LangGraph `InMemorySaver`，只适合单测、临时开发和不需要跨进程恢复的场景。
- `none`：关闭 checkpoint，适合临时排查或不需要恢复能力的任务。

SQLite 路径由 `BS_AGENT_GRAPH_CHECKPOINT_SQLITE_PATH` 控制，默认是 `./bs_agent_checkpoints.sqlite`。相对路径会按项目根目录解析，避免不同启动目录写到不同数据库文件。

## 生产建议

生产环境不要依赖 `memory`。人工审批、节点级恢复和跨进程续跑需要持久化 saver。当前默认 SQLite 已经能提供本地持久化；多实例生产部署时优先接 LangGraph 官方 Postgres saver。
