# 变更记录

本文档记录项目的重要功能变更、架构调整与生产验收情况。

## [未发布]

### 变更

- 应用生命周期统一从 Admin 加载并定时刷新业务意图目录，readiness 报告快照可用性；会话、SSE 与审批恢复在每次 Run 开始时只读取一次不可变 catalog 快照，刷新失败保留旧快照并执行脱敏日志记录。
- 新增 Admin 业务意图 Provider 与不可变运行时快照，通过深度防御性副本隔离查询结果，支持严格校验、稳定 digest 和失败保留旧目录。
- 新增开放式任务理解与动态规划实施计划，按 Admin Intent 快照、`TaskBrief`、动态证据通道、Planner 有界重规划和 Markdown 层级分步交付。
- 新增开放式任务理解与动态规划设计：Admin 成为业务意图 Profile 唯一事实源，本地 `business_intents.json` 将被删除；未命中的投研问题由开放式 `TaskBrief` 表达目标、实体、关键词与时效需求，Planner 动态选择 MCP、Web Search 或 RAG 并生成多步计划，Tool Agent 不再内置专用实体二次探测。
- A 股投研任务的首轮渐进式工具披露若未命中实体解析能力，Planner 会在同一租户 RBAC 与披露上限内定向补充检索 `stock.resolve_company`，避免动态证据规划因 Top-K 偶然遗漏而退化为固定 Skill。
- 投研回答的标准章节、同义章节标题与免责声明统一输出为 Markdown 二级标题，LLM 回答、修复回答与确定性降级回答共用同一格式化出口。
- 生产 Backend 镜像在构建阶段预下载 FastEmbed 中文模型，并将只读模型缓存复制到最终镜像；运行时强制 `local_files_only`，避免容器启动或请求期间访问模型仓库。
- Python 3.11 生产依赖锁补齐 `bm25s`、`fastembed`、`numpy` 及其传递依赖，保证渐进式工具检索可在锁定依赖的容器中安装和预热。
- 工具调用文档补充渐进式披露全链路、双重 RBAC、目录摘要固定、搜索预算、BM25 降级、进程内缓存和生产评测方式。
- `search_tool` 执行后会将受权限约束的命中工具合并到 Graph State 并回到 Planner 重新规划；每次搜索使用独立可重放 call ID，失败尝试也计入预算。
- 工具发现回路最多执行 `BS_AGENT_TOOL_SEARCH_MAX_CALLS` 次；预算耗尽后以 `tool_search_budget_exhausted` 可解释错误结束，中途搜索失败只进入观测事实，不污染后续成功回答。
- Tool Agent Trace 新增脱敏工具发现摘要，仅保存检索模式、命中数、工具名、目录 digest、计数与降级状态。
- Planner 改为基于用户问题、Intent 与可信 `SkillCard` 摘要执行首轮工具检索，仅向 LLM 披露 Top-K 业务工具 Schema 与固定 `search_tool`，单次 Run 最多累计披露 16 个业务工具。
- 通用工具规划改为 `execute/search/respond` 结构化决策，工具版本、风险等级与参数 Schema 继续由 Harness 校验；模型猜测的未披露工具不会从完整 Registry 补查或执行。
- 默认 `rag_search` 现在与 MCP、Web Search 和 `search_tool` 一同在 Tool Catalog 启动期冻结，Planner 搜索索引与 Tool Agent Registry 共用同一 `catalog_digest`。
- Intent Classifier 不再接收或序列化完整工具目录，仅基于用户目标、业务意图与租户 `SkillCard` 分类；工具选择留给 Planner 的渐进式披露流程。
- 工具目录运行时现以最终 `catalog_digest` 原子构建并发布检索索引与 `search_tool`，复用有界 Query Embedding LRU，构建失败时 readiness 保持不可用且不暴露半成品快照。
- 新增受 RBAC 与 Skill 工具白名单约束的 `ToolSearchService` 和本地 `search_tool`；Gateway 仅向可信本地执行器注入工具执行上下文，MCP 工具调用接口保持不变。
- 新增工具检索文档、中英文词元化、FastEmbed 向量封装与 BM25S/Dense RRF 混合索引，支持精确工具名快速路径、权限集合预过滤和 Dense 故障降级。
- 新增渐进式工具检索的配置、依赖与冻结权限上下文契约，约束查询输入、命中结果、检索模式及生产环境本地模型加载策略。
- 新增低延迟渐进式工具披露实施计划，明确混合检索、权限过滤、目录原子绑定、Planner 结构化决策、受控重规划、生产预热与质量门槛的分步实现路径。
- 新增低延迟渐进式工具披露设计：以内置 `search_tool`、BM25 与中文稠密向量混合检索替代向 Intent/Planner 注入全量工具 Schema，并保留现有 Registry、RBAC、审批和 MCP Gateway 执行边界。
- 候选治理持久化收敛为 `MemoryCandidateRepository`，不再使用易与 LangGraph Store 混淆的 `SqlAlchemyMemoryStore` 命名；长期偏好的生效与召回仍由 `PostgresStore` 负责。
- 统一生产长期记忆配置为 `BS_AGENT_MEMORY_STORE_POSTGRES_URL`，业务 ORM、PostgresSaver 与 PostgresStore 的 DSN 均由受管 Secret/env 显式注入。
- 本地 `npm run dev` 在启动 Backend 前自动拉起并等待 Compose PostgreSQL，然后注入业务 ORM、PostgresSaver 和 PostgresStore 的本地 DSN；一键停止同步关闭该容器。
- 更新记忆、存储、Graph 与 Checkpoint 中文文档：Store 管理长期跨会话偏好，Checkpointer 管理 Graph State/Interrupt，Message/Run/Candidate/Approval 业务表仅作为事实或最小治理投影；Approval 文档字段与精简后的模型保持一致。
- 文档明确 Graph 无固定 Memory 节点，长期偏好在业务提交后执行至少一次幂等同步，正常审批恢复只使用 `Command(resume=...)`，`as_node` 仅限 Time Travel 或受控运维。
- 补充破坏性迁移说明：迁移会删除 `experience_entries` 全部数据，上线前必须排空在途审批。
- Alembic 日志初始化保留进程中已有的应用 Logger，避免迁移命令在同进程执行时禁用后续运行日志。
- 清理短期记忆测试中的失效 `memory_manager` 节点夹具，统一使用当前图节点；生产代码扫描仅保留迁移与历史模型中用于兼容清理的 `experience_entries` 表名。
- 新增记忆候选提交后同步补偿器：以稳定 `memory_key` 写入官方 LangGraph Store，按本次 run 有界领取可生效偏好候选；失败脱敏记录并支持覆盖式补偿重试。
- 会话普通请求、SSE 与审批恢复均在业务消息/审批事务提交后触发记忆同步，Store 异常不回滚已提交的业务状态。
- 会话 API 测试增加已提交候选同步到 Store 的断言。
- 同步器初始化或补偿数据库异常同样隔离于业务请求，服务端日志仅记录异常类型。
- 新增真实 PostgreSQL 业务 schema 与官方 PostgresStore 集成测试，覆盖未提交不可见、审批门控和稳定键覆盖写。
- 补充记忆同步质量审查测试：覆盖有界历史补偿、遗留 `syncing` 回收、启动顺序与失败隔离，以及 PostgreSQL 多实例 `SKIP LOCKED` 竞争。
- 应用在 Store 打开后执行一次有界历史补偿，请求提交后机会式重试少量其他 run；启动与补偿异常安全记录并交由 readiness 反映依赖健康。
- 启动测试覆盖 Store 打开失败仍可启动，日志不包含连接串或凭证。
- 失败状态补偿更新同样锁定候选行，避免覆盖其他实例刚完成的同步；测试验证机会式补偿保持跨租户命名空间隔离。
- 明确标注启动与提交后补偿边界的外部异常隔离意图，并整理新增模块导入顺序。
- 补齐生命周期测试中的 Memory Store readiness 假对象契约。
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

- 移除 Redis `ShortTermMemoryStore`、会话完成事件写入、专用配置与测试；Agent 短期记忆统一由 LangGraph `PostgresSaver` 管理。
- 移除生产 Compose 中旧的长期记忆 backend/DSN 变量和现行文档中的固定 Memory 节点、自建长期记忆适配层及过期本地并发说明。
- 移除 SQLAlchemy/自建 Experience Store 双后端配置和对应单元测试。
- 移除 SQLite Checkpointer 配置、实现、依赖及相关运行时文档；测试需要 Checkpointer 时显式注入 `InMemorySaver`。

### 测试

- 真实 SSE 链路验证 A 股公司分析进入 `tool_execution`，动态生成 6 个执行步骤并调用 5 个 MCP 工具；单工具超时或数据源权限不足不中断 Run，最终回答以 Markdown 完成并持久化。
- Planner 回归覆盖首轮 Top-K 未命中实体解析工具时的定向补充披露，并验证投研任务继续进入动态证据规划。
- Synthesizer 单元回归覆盖宽泛公司分析的 Markdown 章节契约，并保留合法的同义标题文案。
- 新增 100 条中文投研工具检索评测集与显式质量门禁，要求 `Recall@5 >= 95%`、缓存预热后的搜索 `p95 <= 150ms`；普通测试不触发模型下载与性能断言。
- 部署测试新增 FastEmbed 模型构建期预热、最终镜像缓存复制、离线运行变量和生产锁依赖检查。
- 资产模块生产配置测试夹具补齐工具检索离线模型约束，确保全量测试进入目标业务断言。
- Backend 全量回归通过：`1010 passed, 34 skipped`；真实中文模型工具检索质量与延迟门禁通过。
- 新增 `search_tool -> Planner` 端到端回路测试，覆盖二次规划、调用 ID 去重、失败后成功、预算终止与 Trace 查询脱敏。
- 新增 Planner 渐进式披露回归测试，覆盖 Top-K Prompt 隔离、总量上限、未披露工具拒绝与默认 RAG 目录同源。
- 本地启动器 `--dry-run --check` 端口预检通过；本地与生产 Compose 配置均完成解析验证。
- 静态检索确认现行配置/文档无旧长期记忆变量、过期本地存储描述和固定 Memory 节点；部署测试验证三类 PostgreSQL DSN 均为必填外部变量；Graph 仍同时编译 `checkpointer` 与 `store`，审批生产路径无 `update_state/as_node`。
- 旧 SQLite 迁移回归测试固定验收到其最后兼容版本 `20260802_0012`，PostgreSQL-only 的原生记忆审批迁移由真实 PostgreSQL 测试独立验收；生产门禁夹具同步到精简后的 Approval 投影。
- Graph Runtime 审批测试按 `Interrupt ID + action_digest` 从 checkpoint 历史精确定位恢复坐标，并覆盖同一线程产生后续中断时旧恢复目标不漂移的回归场景。
- 对齐 Graph Runtime 审批恢复断言：以 `approval_status`、最终响应和已批准调用作为决定事实源，并验证独立副作用步骤具有不同 digest、Interrupt 与 approval。
- 迁移 Graph Runtime 审批恢复测试至真实 checkpoint tuple 坐标、Interrupt 标识与 `action_digest` 契约，保留目录、绑定、多步骤、副作用和 RAG 风险语义。
- 对齐会话 trace 查询测试与 Planner/Synthesizer 按需 Store 召回产生的安全 `experience_memory` 事件序列。
- 新增真实 PostgreSQL 审批恢复 claim 集成测试：隔离 schema 下以两连接并发竞争验证单 owner、过期租约 generation fencing、旧 owner 心跳/完成拒绝，以及终态同决定幂等与异决定冲突。
- 真实 PostgreSQL `PostgresSaver` 审批测试改为贯穿项目 Graph、会话投影、ApprovalRepository 与恢复入口，Checkpointer 和业务审批投影均使用隔离 PostgreSQL schema，并完整校验禁存字段不存在；同时覆盖精确 checkpoint/Interrupt/digest 投影、恢复和错配拒绝。
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
