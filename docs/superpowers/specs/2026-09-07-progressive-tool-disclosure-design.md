# 低延迟渐进式工具披露设计

## 背景

Agent 启动后会从 MCP Server 动态发现工具。Tushare MCP 当前可暴露数百个工具，如果将完整 `ToolSpec` 全量传给 Intent 或 Planner，会持续占用上下文、增加模型延迟，并降低相似工具之间的选择准确率。

本设计在保留现有 `ToolRegistry`、MCP Gateway、RBAC、审批、审计和目录快照约束的基础上，引入项目内置只读工具 `search_tool`。工具检索采用成熟开源组件实现轻量混合检索，项目只负责索引编排、权限过滤和上下文披露，不自行实现 BM25、Embedding 模型或向量算法。

## 目标

- Intent 和 Planner 不再接收全量 Tool Schema。
- Planner 每次只接收与当前任务相关的少量完整 Tool Schema。
- 正常首轮规划不因工具检索增加一次 LLM 往返。
- Planner 在后续重规划时仍能通过 `search_tool` 主动发现其他工具。
- 单次 Run 固定使用同一个工具目录版本，避免检索结果与执行器不一致。
- 权限不足的工具不得出现在检索结果或模型上下文中。

## 非目标

- 不使用 `langgraph-bigtool` 接管现有 Agent Graph。
- 不引入 Elasticsearch、Qdrant、Milvus 或独立搜索服务。
- 不修改 Skill Registry、RAG、记忆系统、前端和业务数据库。
- 不使用额外 LLM 进行工具选择或重排。
- 不替换现有 MCP Gateway、审批和工具执行治理链路。

## 核心决策

### 内置 `search_tool`

`search_tool` 是项目内置的低风险只读工具，也是工具目录的唯一模型可见入口。它接收当前任务或子任务描述，返回有界的工具能力卡片；结果不包含连接信息、凭据和未授权工具。

正常首轮由 Harness 在调用 Planner 前，使用与 `search_tool` 相同的检索服务完成一次预取，并把命中工具的完整 Schema 注入 Planner。因此首轮不需要模型先调用一次 `search_tool`。

当 Planner 根据工具结果重规划并发现当前工具集合不足时，可以显式调用 `search_tool`。Harness 根据新的检索结果扩充本次 Run 的已披露工具集合，再进入下一轮规划。该路径允许增加一次模型往返，但只在确有需要时发生。

### 完整目录与模型上下文分离

完整 `ToolRegistry` 继续保存在执行侧，供以下内部能力使用：

- 工具身份和版本校验。
- `catalog_digest` 计算与 Run 级固定。
- MCP 执行器绑定。
- RBAC、风险、审批和参数校验。
- Skill 契约的内部一致性检查。

Intent 不再接收 `available_tools` 全量 Schema。Planner 只接收：

- `search_tool` Schema。
- 当前检索命中的 Top-K 完整 Tool Schema。
- 当前 Run 之前已经披露且仍被计划引用的 Tool Schema。

不得在检索失败时回退为向模型暴露全部工具。

## 组件边界

### `ToolSearchDocument`

由启动期发现的 `ToolSpec` 生成，不额外维护供应商工具映射。检索文本由以下字段组成：

- 规范化工具名。
- 工具描述。
- 参数名和参数描述。
- MCP Server 名称。
- ToolSpec 已存在的能力、风险和权限元数据。

完整输入 Schema 保留在 Registry，不直接进入稀疏或稠密检索文本。

### `ToolSearchIndex`

负责构建和持有不可变检索索引：

- 稀疏检索使用 `bm25s`。
- 中文稠密向量使用 FastEmbed 的 `BAAI/bge-small-zh-v1.5` ONNX 模型。
- 目录向量只在 `catalog_digest` 变化时重新生成。
- 246 量级的工具向量直接保存在进程内矩阵中，不引入向量数据库。
- 新索引完整构建成功后才原子替换旧索引。

FastEmbed 模型作为进程级单例预热，不按请求加载。模型文件在生产镜像构建阶段准备，运行期使用本地文件，禁止首个用户请求触发在线下载。

### `ToolSearchService`

提供与传输无关的检索接口，由 Harness 首轮预取和 `search_tool` 执行器共同复用。它负责：

- 校验查询和 Top-K 上限。
- 按租户和用户 Scope 生成可见工具集合。
- 执行混合检索与排名融合。
- 返回稳定工具 ID、摘要、排名来源和目录 digest。
- 根据稳定工具 ID 从同一 Registry 快照加载完整 Schema。

### Planner 工具上下文构建器

接收本次检索结果和 Run 已披露集合，只组装当前规划所需的 Schema。它不负责搜索、不连接 MCP，也不执行工具。

## 检索策略

### 快速路径

查询明确包含规范化工具名，且该工具对当前 Principal 可见时，直接命中，不执行 Embedding。

### 混合路径

其余查询执行：

1. BM25 召回 Top 20。
2. 稠密向量召回 Top 20。
3. 两路可以并行执行。
4. 使用 Reciprocal Rank Fusion 合并排名，默认常数 `k=60`。
5. 返回 Top 5，配置允许范围为 1 至 8。

不直接加权 BM25 与余弦相似度，因为两者分值尺度不同。第一版不增加 Cross Encoder 或 LLM Reranker。

### 查询来源

首轮查询由用户问题、Intent 的业务域以及命中的 Skill 摘要组成。多步任务后续使用 Planner 当前子任务作为查询，不重复使用宽泛的原始问题。

不得由固定的 Tushare API 名称枚举生成查询，也不得维护口语到供应商 Tool 名称的手工映射。

## 请求流程

```text
应用启动
-> MCP tools/list
-> ToolSpec 校验并生成冻结 ToolRegistry
-> 根据 catalog_digest 构建 ToolSearchIndex
-> 索引和 Embedding 模型预热成功
-> readiness ready

用户请求
-> Intent Router（不含全量工具目录）
-> Harness 调用 ToolSearchService 首轮预取
-> RBAC 过滤
-> 加载 Top-K 完整 Tool Schema
-> Planner 生成计划
-> Tool Agent 经 MCP Gateway 执行
-> Observe / Replan
-> 如工具不足，Planner 调用 search_tool
-> 扩充本 Run 已披露集合后继续规划
```

## 缓存与一致性

- 进程内缓存不可变 `ToolRegistry`、BM25 索引、工具向量矩阵和模型实例。
- 查询向量使用有界 LRU 缓存，缓存键包含规范化查询和 `catalog_digest`。
- Redis 不进入每次工具搜索的热路径，也不保存执行器、MCP Session 或凭据。
- 第一版不新增 Redis 工具目录缓存；启动期仍以 MCP 动态发现结果为事实源。
- 单次 Run 固定 `catalog_digest`，运行中目录刷新不改变该 Run 的 Registry 和已披露集合。
- 目录刷新失败时保留上一份已验证快照，不发布半成品索引。

## 权限与安全

- `search_tool` 本身为低风险只读工具，不要求人工审批。
- 检索前先应用租户、用户 Scope、Skill 约束和数据源启用状态。
- 未授权工具不得通过名称、描述、参数或命中数量侧信道暴露给模型。
- MCP Gateway 在执行时再次校验权限、风险、参数、审批状态和目录 digest。
- Trace 只记录检索耗时、候选工具稳定 ID、排名来源和 digest，不记录凭据和完整输入 Schema。

## 预算与终止条件

- 首轮 Harness 预取最多一次。
- 单次 Run 中 Planner 主动调用 `search_tool` 最多 4 次。
- 每次最多披露 8 个工具。
- 单次 Run 累计披露工具上限为 16 个。
- 达到搜索预算后，不得扩大到全量目录；Planner 必须使用现有工具、请求澄清或明确说明能力不足。
- Graph 原有 recursion limit 继续作为最终循环上限。

## 失败处理

- MCP 启动发现失败：生产 readiness 失败。
- ToolSearchIndex 首次构建失败：生产 readiness 失败，不向模型回退注入全量工具。
- 稠密检索运行时失败：记录脱敏 Trace，本次搜索降级为 BM25；不得绕过 RBAC。
- BM25 和稠密检索同时失败：返回结构化 `tool_search_unavailable`，由 Planner 请求澄清或说明暂不可用。
- 检索无结果：返回空集合，不猜测工具名，不执行相似但业务无关的工具。
- 目录 digest 不一致：拒绝执行并触发受控重规划，不在旧计划上替换工具。

## 可观测性

新增以下有界指标：

- 工具索引构建耗时和工具数量。
- `search_tool` 总耗时、BM25 耗时、Embedding 耗时与融合耗时。
- 首轮命中率、主动二次搜索率、空结果率和降级率。
- 每个 Run 的披露工具数量和搜索次数。
- 计划选中工具是否位于检索 Top-K。

Node Trace 继续只用于调试和观测，不在产品端展示完整工具目录或内部排名细节。

## 测试与验收

### 单元测试

- ToolSpec 能稳定生成检索文档。
- 精确工具名走快速路径且不调用 Embedding。
- BM25 与向量结果按 RRF 稳定融合。
- RBAC 在返回结果前过滤未授权工具。
- Top-K、累计披露和搜索次数预算生效。
- 稠密检索失败时只降级到 BM25。
- digest 变化触发新索引，旧 Run 保持原快照。

### 集成测试

- MCP 动态发现结果可以建立索引并通过 `search_tool` 找回。
- Intent 和 Planner 请求中不存在全量 Tool Schema。
- Planner 只获得 Top-K 完整 Schema，Tool Agent 仍能从完整 Registry 执行。
- 后续子任务可以再次搜索并扩充已披露集合。
- 检索失败不会退回全量工具，也不会绕过 MCP Gateway。

### 质量门槛

- 建立不少于 100 条中文投研工具检索样例，覆盖公司基础信息、财务报表、指标、行情、公告、业绩和新闻场景。
- 发布门槛为 `Recall@5 >= 95%`，并保留 BM25、Dense、Hybrid 三组对照结果。
- 在预热后的标准开发环境中，246 个工具的检索 `p95 <= 150ms`，该指标不包含 LLM 和 MCP 业务工具执行耗时。
- 模型请求快照测试证明全量 Tool Schema 不再进入 Intent 或 Planner 上下文。

## 依赖选择

- `bm25s`：稀疏 BM25 检索。
- `fastembed`：ONNX 中文稠密向量生成。
- `numpy`：小规模向量矩阵与余弦相似度计算。
- 现有 LangGraph、ToolRegistry 和 MCP Gateway：图编排、目录快照与受控执行。

所有新增依赖必须固定兼容版本范围，并经过许可证、漏洞和构建体积检查。
