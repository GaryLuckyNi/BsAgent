# 变更记录

本文档记录项目的重要功能变更、架构调整与生产验收情况。

## [未发布]

### 变更

- 新增内部会话上下文压缩：`agent_messages` 继续保存完整原始会话，迁移 `20261010_0014` 新增只追加的 `agent_context_compactions`、active 指针、previous 链和来源 manifest；模型读取 active 摘要与边界后的原始消息，不改变消息分页与审批 Checkpoint 语义。
- 上下文压缩默认关闭。启用后在 70% 窗口水位向 Celery `context-compaction` 队列派发预压缩任务，在 85% 水位同步兜底并以 50% 为目标；生产观测只记录哈希会话标识、压缩 ID、token、比率、耗时和结果，不记录原文或摘要正文。
- 回滚上下文压缩时将 `BS_AGENT_CONTEXT_COMPACTION_ENABLED=false` 并重启 API 与 worker，系统恢复最近 20 条完成消息的兼容读取路径；保留 compaction 数据用于审计和再次启用，不执行数据删除。
- Planner 增加批次反馈决策，模型选择 continue/revise/finish；取消按业务错误自动重规划和按拓扑删除末端任务，修复计划修订丢失检索提示。计划新增执行类型与完成条件，修订保留历史并显式复用已完成成果。
- 工具发现收敛为权限与 Skill 作用域过滤、工具名精确匹配和 BM25 排序，删除 Dense、RRF、FastEmbed 模型与向量缓存；专业候选与 web_search 可共同披露，模型通过 auto 自主选择是否调用。搜索配额改为调用前原子预留，并修复并行检索状态合并冲突。
- Skill 发现收敛为 `domain/subdomain` 作用域过滤后披露全部轻量卡片，由 Planner 自主选择，不再维护 Skill BM25 Top-K。
- Tool Agent 失败结果保留为观察，不再通过整节点重试重复模型决策。审批恢复新增规划策略版本校验；旧策略 Checkpoint 需要重新发起请求。
- 本地真实模型试运行发现反馈 JSON 不合法会导致 Planner 500，已改为一次修复后安全停止并显式说明未核实目标；反馈提示改为精简的 continue/revise/finish 格式。`auto` 一次返回多个工具调用时增加一次模型修复，不由代码代选工具。
- MCP 通用执行失败增加安全阶段分类，并在 Trace 只展示允许的阶段和异常类名；本地真实调用确认当前 Tushare 财务指标接口返回远端工具错误，用户确认第三方接口权限不足，未泄露远端原始正文。

- 外部 Tushare MCP 工具发现失败不再阻断整个 Agent 图初始化；工具目录保留 RAG、联网检索等平台能力继续规划，并以不含 URL、Token 或异常正文的安全日志记录 Provider 不可用状态。
- Admin 启用跨资产金融研究意图，补充贵金属、大宗商品、债券、基金、外汇、利率、宏观和资产配置边界；A 股分类保持独立，Planner 仍按分类结果渐进式发现能力。
- AI Research 思考面板新增独立的 Tool/Skill 活动行，展示“正在联网检索”“正在查询市场数据”等用户可理解的摘要及进行中/已完成状态；原始工具名称、节点标签和控制面 Trace 不进入产品界面。
- Agent 的 Tushare MCP 运行链改为由 Admin 市场数据源配置完整 URL，Agent 读取激活配置后直连官方 Streamable HTTP；`BS_AGENT_MCP_SERVER_URL` 仅保留为默认关闭的通用 MCP 扩展，仓库内示例服务只保留显式学习命令。
- Synthesizer 不再向最终模型暴露原始任务 ID、工具名、内部错误码和执行状态；成功结果转换为研究材料，失败维度转换为最小数据局限，部分证据可用时以研究分析而非链路故障报告为主体。新增 AI Research 近期体验验收规范，明确首屏反馈、公开思路、工具活动、原生 Token 流和内部信息隔离边界。
- 修复 AI Research 长答案流式渲染中断：开启 LangGraph SDK `useStream` 原生 `throttle` 合并同一批 Token 的同步外部 Store 通知，并对等价答案快照增加幂等同步，避免 React `Maximum update depth exceeded` 导致页面只显示部分答案；重规划时的完全相同公开思路摘要不再重复拼接。
- Analysis Agent、Skill Agent 和 Synthesizer 在 Thinking 消耗输出预算但没有返回正文时，允许一次关闭 Thinking 的有界补偿请求；同时纯概念投研问题默认收敛为单个分析任务，避免无能力依赖时过度拆分串行任务。
- 修复 Agent 思考与答案的流式边界：产品端仅接收中文、面向用户的分析摘要，不再透传 Intent、Planner、Skill、Analysis 或 Synthesizer 的私有 `reasoning_content`，避免暴露 JSON 契约、内部字段、任务 ID 与中英混排控制指令；最终答案继续按原生 token 流式输出，前端按本轮 Message ID 隔离历史消息。
- DeepSeek 流式结果新增 `finish_reason` 保留，Planner 首次输出预算提升至 4096；遇到 `length` 截断、空正文或无效计划时执行一次关闭 thinking 的结构化补偿重试，避免推理耗尽共享输出预算后直接中断节点。
- Planner 明确单一职责拆分约束：同一目标若同时需要 Skill 和 Tool，必须拆成有依赖关系的独立子任务，由 Tool 获取事实，再由 Skill 或分析任务消费前序结果。
- 修复前端中断 Agent Streaming Protocol 或 AG-UI 消费后未显式关闭上游会话流的问题；StreamingResponse 通过可关闭的异步线程桥接消费同步 Graph 流，确保连接取消后逐层关闭适配器、执行会话失败收口并释放 `active_run_id`，避免会话永久返回“请求正在执行或会话忙”。
- 思考面板展示动态生成的中文研究思路摘要，不混入节点进度、阶段任务清单或控制面私有推理，面板标题同步中文化。
- 修复整体市场问题误选单证券数据工具及失败 JSON 直出：web_search 作为受 RBAC 约束的精简平台基础能力常驻披露，由 Planner 根据时效性和证据缺口自主决定是否调用；联网搜索扩展为市场事件、政策、新闻、公告与实体核验通用投研证据能力。数据 Provider 权限不足改为携带 tool_name 的可重规划 Observation，Synthesizer 仅输出去重后的用户友好错误信息。
- DeepSeek 非工具参数生成节点保留原生 Thinking Mode：Provider 区分私有 `reasoning_content`、公开 `content` 与结束原因；AG-UI 仅映射显式公开的分析摘要和最终答案，工具进度保持独立轻量活动提示。
- 新增独立 `/api/ag-ui` 协议适配端点：请求与响应使用官方 `ag-ui-protocol` 类型和 `EventEncoder`，将现有会话流映射为 Run、Step、Activity 与 Text Message 事件；审批中断使用 `RUN_FINISHED(outcome=interrupt)`，标准 `RunAgentInput.resume` 复用既有租户 RBAC、审批投影和 LangGraph `Command(resume=...)` 恢复链路。
- 前端会话流改用官方 `@langchain/langgraph-sdk/react` 的 `useStream + FetchStreamTransport`，删除自建浏览器 SSE 解析器；Backend 在保留现有 FastAPI、JWT、PostgreSQL 会话与幂等生命周期的前提下新增 Agent Streaming Protocol 适配端点。
- Graph 流式模式统一为 `updates + messages + custom`：节点通过官方 `get_stream_writer()` 发出面向用户的结构化进度，Synthesizer 的 LangChain 调用通过 `messages` 模式实时透传 LLM Token，产品端不展示 Node Trace 或私有推理。
- 最终答案已产生原生 Token 时不再按字符二次切片，前端由 SDK 按稳定 Message ID 合并分片，并在最终事件到达时完成状态收口，避免重复渲染答案。
- Tool Agent 将“模型未生成 Tool Call”或“返回错误工具名”转换为带错误码的 `business_failure` Observation，由 Planner 在既有 10 轮预算内重新规划，不再直接使请求返回 500。
- Planner 增加单次工具任务约束：必须能够从用户消息或依赖结果确定必需参数，不允许把“多家公司”等复数抽象对象直接传给单实体工具。
- 业务意图运行时不再将 Admin 返回的空启用目录当作有效快照；首次加载为空时 readiness 明确报不可用，避免所有投研请求被静默误判为范围外。
- Tool Agent 生成参数时只传入用户会话、当前 `task_goal` 和该任务的依赖结果，隔离其他并行分支的 `AIMessage.tool_calls` 与 `ToolMessage`，避免重规划时复用错误工具名。
- DeepSeek Function Calling 改用与同步 LangGraph 节点一致的 LangChain `invoke()`，并对参数生成显式关闭 thinking 模式，避免临时事件循环连接故障及历史 `reasoning_content` 契约冲突。
- Graph 主链新增任务调度器，通过 LangGraph `Send` 并行分发无依赖 Skill、Tool 和 Analysis 子任务，有依赖任务按 DAG 自然串行。
- 子任务执行结果统一为 `TaskResult`：业务失败作为 Observation 回到 Planner 有界重规划，网络与 Provider 短暂故障交给 LangGraph `RetryPolicy`，终止错误不盲目重试。
- Skill Agent 改为按 Planner 选中名称加载完整定义与指南；Analysis Agent 只基于依赖结果执行任务级推理，两者均返回可并行合并的局部 State 更新。
- Synthesizer 改为基于 `goal + TaskResult + Evidence` 动态生成中文 Markdown，不再无条件套用固定 A 股章节；Verifier 收敛为终稿安全、免责声明和产品边界闸门。
- 会话 SSE 产品进度和公开分析思路改为读取当前任务 DAG、活动任务与 Observation，不再依赖已删除的执行模式、固定维度或实体修复字段。
- Trace 摘要对齐 Intent `goal`、Planner DAG 与任务结果，节点在创建 artifact 后异常时会补写脱敏错误事件，避免孤立 artifact 导致持久化身份冲突。
- Tool Agent 改为执行 Planner 下发的单个子任务，通过原生 `tool_calls` 生成参数并统一经过 Tool Gateway；移除旧的固定工具计划、实体猜测与占位符执行逻辑。
- Agent 消息状态迁移至 LangChain 原生消息类型与 LangGraph `add_messages` reducer，并为并行子任务结果和工具调用记录增加幂等合并 reducer。
- 并行 Skill Worker 只返回子任务局部更新，不再写入共享 `selected_skill` 标量字段，避免 LangGraph `Send` 同超步并发合并冲突。
- Planner 改为单一 LLM 任务规划路径：基于 Intent `goal`、双 Top-K Skill/Tool 卡片、长期规划偏好和既有任务 Observation 动态生成最多 12 个子任务的 DAG，不再进入固定知识问答、工具动作或 A 股模板分支。
- 新增严格任务计划契约，支持 Skill、Tool 与无外部能力的分析任务，校验任务 ID、依赖闭包、无环约束和本轮能力披露边界；Planner 决策预算默认固定为 10 轮。
- Skill 渐进式披露按 Admin 配置的 `domain/subdomain` 过滤后直接交给 Planner 选择；轻量卡片移除租户、版本、风险和工具白名单，完整定义仅在 Planner 选中 Skill 后按名称加载当前启用配置。
- Intent Router 收敛为基于 Admin 目录的 LLM 范围分类与 `goal` 改写，删除本地规则预路由、Skill/Tool 候选、风险、澄清和 `TaskBrief` 字段；Graph State 新增独立 Planner 决策计数。
- Admin 业务意图目录收敛为 `intent_type/domain/subdomain` 分类契约，删除候选 Skill、风险等级和所需能力等 Planner 职责字段；新增 Alembic 迁移并同步 CRUD 与管理界面。
- 新增投研意图、动态规划与任务图重构实施计划，按 Admin/Intent 契约、双 Top-K 能力发现、LLM 任务 DAG、原生 ToolMessage、三类 Worker、受限 ReAct 和终稿校验分阶段迁移，并在全链路完成后统一复审。
- 新增投研意图、动态规划与任务图重构设计：Intent Router 收敛为范围判断与 `goal` 改写，Planner 负责 Top-K Skill/Tool 检索和任务 DAG，现有 Analysis Agent 承担无 Skill/Tool 的任务级推理，Verifier 保持终稿质量闸门，不新增独立 Reasoning Agent。
- Planner 依据独立 `TaskBrief` 生成最多 12 个分析问题和对应子任务，不再通过“标准公司快照”提前返回或无条件执行固定五维；未命中 Skill 的 A 股开放任务同样进入动态规划。
- 投研证据通道新增 `evidence_channels` 契约：行情、估值与财务优先使用已披露 MCP 工具，Web Search 仅在 `web_search_required=true` 时按关键词合成恰好一个请求，RAG 仅在 `rag_required=true` 时启用；未披露或 RBAC 不可见工具不会进入 `requested_tools`。
- Intent Classifier 在解析边界兼容非 `task_workflow` 的旧扁平 JSON 输出并规范化为 `decision + task_brief:null`；任务型输出仍必须显式提供有效 `TaskBrief`。
- Intent Router 通过一次 LLM 结构化理解同步输出粗粒度路由与独立 `TaskBrief`；命中 `task_workflow` Profile 时仍理解本次目标、实体、关键词和时效需求，未命中的 A 股投研问法不再被迫扩充细碎 `sub_intent`。
- Graph State 新增独立 `task_brief` 与初始值为 `0` 的 `semantic_replan_count`；Intent Router builder 收敛为显式注入 catalog、LLM 和可用 Skill Provider 的唯一入口。
- 应用生命周期统一从 Admin 加载并定时刷新业务意图目录，readiness 报告快照可用性；会话、SSE 与审批恢复在每次 Run 开始时只读取一次不可变 catalog 快照，刷新失败保留旧快照并执行脱敏日志记录。
- 新增 Admin 业务意图 Provider 与不可变运行时快照，通过深度防御性副本隔离查询结果，支持严格校验、稳定 digest 和失败保留旧目录。
- 新增开放式任务理解与动态规划实施计划，按 Admin Intent 快照、`TaskBrief`、动态证据通道、Planner 有界重规划和 Markdown 层级分步交付。
- 新增开放式任务理解与动态规划设计：Admin 成为业务意图 Profile 唯一事实源，本地 `business_intents.json` 已删除；未命中的投研问题由开放式 `TaskBrief` 表达目标、实体、关键词与时效需求，Planner 动态选择 MCP、Web Search 或 RAG 并生成多步计划，Tool Agent 不再内置专用实体二次探测。
- A 股投研任务的首轮渐进式工具披露若未命中实体解析能力，Planner 会在同一租户 RBAC 与披露上限内定向补充检索 `stock.resolve_company`，避免动态证据规划因 Top-K 偶然遗漏而退化为固定 Skill。
- 投研回答的标准章节、同义章节标题与免责声明统一输出为 Markdown 二级标题，LLM 回答、修复回答与确定性降级回答共用同一格式化出口。
- 生产 Backend 镜像不再下载或复制工具向量模型，工具目录首次构建与后台刷新只建立轻量 BM25 索引。
- Python 3.11 生产依赖锁移除 `fastembed`、ONNX Runtime 和模型下载依赖；`numpy` 仅作为 `bm25s` 的传递依赖保留。
- 工具调用文档补充渐进式披露全链路、双重 RBAC、目录摘要固定、搜索预算、BM25 降级、进程内缓存和生产评测方式。
- `search_tool` 执行后会将受权限约束的命中工具合并到 Graph State 并回到 Planner 重新规划；每次搜索使用独立可重放 call ID，失败尝试也计入预算。
- 工具发现回路最多执行 `BS_AGENT_TOOL_SEARCH_MAX_CALLS` 次；预算耗尽后以 `tool_search_budget_exhausted` 可解释错误结束，中途搜索失败只进入观测事实，不污染后续成功回答。
- Tool Agent Trace 新增脱敏工具发现摘要，仅保存检索模式、命中数、工具名、目录 digest、计数与降级状态。
- Planner 改为基于用户问题、Intent 与可信 `SkillCard` 摘要执行首轮工具检索，仅向 LLM 披露 Top-K 业务工具 Schema 与固定 `search_tool`，单次 Run 最多累计披露 16 个业务工具。
- 通用工具规划改为 `execute/search/respond` 结构化决策，工具版本、风险等级与参数 Schema 继续由 Harness 校验；模型猜测的未披露工具不会从完整 Registry 补查或执行。
- 默认 `rag_search` 现在与 MCP、Web Search 和 `search_tool` 一同在 Tool Catalog 启动期冻结，Planner 搜索索引与 Tool Agent Registry 共用同一 `catalog_digest`。
- Intent Classifier 不再接收或序列化完整工具目录，仅基于用户目标、业务意图与租户 `SkillCard` 分类；工具选择留给 Planner 的渐进式披露流程。
- 工具目录运行时以最终 `catalog_digest` 原子构建并发布 BM25 检索索引与 `search_tool`，构建失败时 readiness 保持不可用且不暴露半成品快照。
- 新增受 RBAC 与 Skill 工具白名单约束的 `ToolSearchService` 和本地 `search_tool`；Gateway 仅向可信本地执行器注入工具执行上下文，MCP 工具调用接口保持不变。
- 工具检索文档采用确定性的中英文词元化与 BM25 索引，支持精确工具名快速路径和权限集合预过滤。
- 渐进式工具检索契约约束查询输入、命中结果、检索模式及冻结权限上下文，不再包含本地向量模型加载策略。
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

- 移除固定 A 股证据规划、固定回答模板、逐工具请求执行模式和未接入新 Graph 的 Retriever Agent 兼容分支；`rag_search` 改为与 MCP/Web Search 一致的普通 Tool Agent 执行路径。
- 移除本地 Skill 基类和 `ConfiguredSkill` 双轨运行时，Skill Registry 只保留能力卡片发现与完整定义分层加载。
- 移除冗余的 `build_llm_intent_router_node` 及可绕过显式 catalog 绑定的 Router builder 测试替身入口。
- 移除本地业务意图 JSON 事实源及 `load_default()`、`load_from_path()` 文件加载入口；Intent Router 与节点构造链路统一显式接收 Admin catalog 快照，不再提供空目录、文件或全局单例回退。
- 移除 Redis `ShortTermMemoryStore`、会话完成事件写入、专用配置与测试；Agent 短期记忆统一由 LangGraph `PostgresSaver` 管理。
- 移除生产 Compose 中旧的长期记忆 backend/DSN 变量和现行文档中的固定 Memory 节点、自建长期记忆适配层及过期本地并发说明。
- 移除 SQLAlchemy/自建 Experience Store 双后端配置和对应单元测试。
- 移除 SQLite Checkpointer 配置、实现、依赖及相关运行时文档；测试需要 Checkpointer 时显式注入 `InMemorySaver`。

### 测试

- 新增上下文压缩策略、迁移、谱系与并发激活、摘要安全契约、有效上下文装配、三种 API 入口、Celery 派发、无正文可观测性和 8 类离线质量评测覆盖；真实 DeepSeek 质量评测通过 `BS_AGENT_RUN_LIVE_CONTEXT_COMPACTION_EVAL=1` 显式启用。
- 新增 AG-UI 类型化事件、流式文本、错误终态、审批中断与标准恢复载荷的单元及 API 集成覆盖。
- 新增真实 StateGraph 动态工具端到端测试，覆盖意图分类、Top-K 披露、Planner 选择、原生 `tool_calls/ToolMessage`、任务合并、Markdown 合成和 Verifier。
- 新增并行 `Send` 任务、依赖调度、业务重规划、基础设施重试分类、高风险调用精确恢复和动态终稿的回归覆盖。
- 动态 Planner 回归覆盖纯推理任务、Skill/Tool 互斥、重复与未知依赖、自依赖、循环依赖、任务数量上限、轻量候选 Prompt、未披露能力拒绝及 Planner 节点状态写入。
- Skill Registry 与检索回归覆盖领域资格过滤、Top-K 相关性排序、当前定义分层加载、风险延迟披露和注册中心失败边界。
- Planner 回归证明“海能达是否适合买入”与“只分析财务质量”产生不同分析问题、子任务和工具集合，并覆盖单次 Web Search、条件 RAG、12 条问题上限及未披露工具隔离。
- Graph Runtime 测试夹具对齐 `decision + task_brief` 新契约，覆盖 run、stream 与直接 build 入口，同时保留生产环境缺少 Intent LLM 时的严格失败语义。
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
