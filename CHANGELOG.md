# 变更记录

本文档记录项目的重要功能变更、架构调整与生产验收情况。

## [未发布]

### 变更

- 工具调用与中高敏记忆候选统一使用 LangGraph Interrupt 完整保存动作，业务审批记录仅保存最小投影；恢复按精确 checkpoint、Interrupt 与动作摘要校验后仅使用 `Command(resume=...)`。
- 显式偏好候选统一拒绝包含中英文问号的疑问句，避免将询问误判为稳定偏好。
- 长期偏好读取会忽略畸形 Store 条目并安全降级搜索、迭代与解码异常；偏好候选仅接受用户明确指令或第一人称稳定偏好陈述。
- 区分规划偏好与呈现偏好，关注领域仅约束 Planner，不再进入 Synthesizer 的回答策略。
- Planner 与 Synthesizer 按需通过 LangGraph 原生 Runtime Store 读取租户/用户严格隔离的长期偏好；Planner 生成稳定、可合并的用户明确偏好候选，读取失败安全降级。
- API 会话与审批恢复链路统一注入官方 LangGraph `BaseStore`，移除已失效的 ExperienceStore 召回、双写与最终提交路径；业务库继续保留 Memory Candidate 审计记录。
- Graph 组合根改为接收并编译官方 LangGraph `BaseStore`，移除固定 Memory 节点与过渡检索器参数；Harness 已支持透明传递 LangGraph runtime。
- Agent State、Skill 输入与追踪摘要移除 `memory_context`，审批协调节点不再直接执行最终经验持久化。
- 应用生命周期关闭资源时隔离单个关闭异常，记录异常后继续释放其余资源，并在完成后传播首个关闭失败。
- 删除自建长期经验 Store、适配器、检索与提交栈，应用生命周期、依赖注入和健康检查统一使用官方 `PostgresStoreRuntime`。
- 长期记忆 PostgreSQL 配置统一为 `memory_store_postgres_url`；本地、测试和生产环境均拒绝空值或非 PostgreSQL URL。
- 精简记忆候选与审批持久化：候选记录新增 Store 同步状态，审批记录收敛为原生恢复所需投影；迁移会拒绝存在在途审批的数据库，并明确经验表历史数据不可恢复。
- 将长期记忆运行时收敛为单一官方 `PostgresStore` 资源，并新增基于独立 PostgreSQL schema 的真实 Store 集成测试基座。
- 新增 LangGraph Store 记忆与审批原生化重构实施计划，按 PostgreSQL 测试基座、表结构精简、Graph Runtime、Interrupt 审批、写后同步和生产验收分步执行。
- 新增长期经验记忆与审批原生化重构设计，明确采用 LangGraph 原生 `Store + Runtime`、删除固定 Memory 节点、精简审批投影并统一使用 PostgreSQL Store。
- 将过重的 Planner 节点拆分为节点编排、A 股证据规划、通用工具规划、稳定计划模板和 LLM JSON 解析五个独立边界。
- 保留 `app.graph.nodes.planner_agent` 兼容入口，不改变 LangGraph 节点名、状态契约、计划字段和路由行为。
- 意图分类在调用 LLM 时按租户注入精简的 `SkillCard`，仅暴露可选 Skill 的标识、摘要、支持意图、标签与风险等级；注册中心暂不可用时降级为空列表。
- LangGraph 运行配置增加 `BS_AGENT_GRAPH_RECURSION_LIMIT`，默认限制为 32 个图超步，防止条件边或重试链路无界循环。
- Graph Checkpointer 统一收敛为官方 `PostgresSaver` 与 PostgreSQL 连接池，运行时必须提供 PostgreSQL DSN。

### 移除

- 移除 SQLAlchemy/自建 Experience Store 双后端配置和对应单元测试。
- 移除 SQLite Checkpointer 配置、实现、依赖及相关运行时文档；测试需要 Checkpointer 时显式注入 `InMemorySaver`。

### 测试

- 新增真实 PostgreSQL `PostgresSaver` 审批中断生命周期测试，覆盖精确 checkpoint/Interrupt/digest 投影、恢复和错配拒绝。
- 会话与审批 API 测试迁移为最小审批投影，并覆盖同步、流式中断的稳定身份和无敏感 payload 返回。
- 新增 Planner 模块边界测试，防止后续将投研规划、工具校验和静态模板再次耦合回节点文件。
- 补充意图分类 Skill 上下文、图递归上限与 PostgreSQL Checkpointer 的单元及集成测试。

## [2026-08-13]

### 新增

- 新增 LangGraph `PostgresStore` 长期经验记忆运行时，支持租户、用户命名空间与记忆检索。
- 新增 Tool Catalog 运行时快照，集中管理 MCP 工具发现、刷新、健康状态与调用入口。
- 新增 Memory 与 Tool Catalog 的生产配置、健康检查和单元测试。
- 新增 MCP Server 容器化构建与 Streamable HTTP 端到端验证。

### 变更

- MCP 传输由 `stdio` 升级为官方 Streamable HTTP，Agent 通过 LangChain MCP Adapter 进行启动期动态工具发现。
- 长期经验记忆统一交由 LangGraph Store 持久化，保留 PostgreSQL 业务表作为审批候选与审计记录。
- 会话、审批恢复、意图路由和健康检查接入新的 Memory 与 Tool Catalog 运行时。
- 更新本地一键启停脚本、生产 Compose 编排、环境变量示例和相关中文架构文档。

### 移除

- 移除 MCP `stdio` 客户端、服务端入口及其对应测试。

### 验证

- Backend：`959 passed, 14 skipped`。
- MCP Server：`121 passed, 1 skipped`。
- Ruff 静态检查通过。
- 生产 Compose 配置解析通过。
