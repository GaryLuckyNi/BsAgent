# 轻量能力发现设计

## 背景

当前工具发现同时维护 BM25 索引、稠密向量、RRF 融合、查询向量缓存和文档向量磁盘缓存。该设计提高了语义召回能力，但 FastEmbed 模型在全新环境、模型修订变化或缓存失效时会引入约 20～30 秒冷启动，并扩大工具目录刷新、镜像构建和故障恢复的复杂度。

本项目的工具文档是由工具名、能力说明、参数名和参数说明组成的短结构化文本。Planner 或 Tool Agent 还会生成面向能力的 `tool_search_query`，并由模型对候选工具做最终选择。因此，工具检索只需可靠地缩小候选集，不需要承担完整的语义理解。

Skill 候选数量更少，且已经由 `domain` 和 `subdomain` 形成清晰作用域。对 Skill 再执行文本排序会提前隐藏能力，也会把本应由 Planner 完成的选择固化到检索规则中。

## 目标

- 工具发现仅使用作用域过滤、工具名精确匹配和 BM25 排序。
- Skill 仅按启用状态和业务作用域过滤，将全部符合条件的轻量卡片披露给 Planner。
- 是否调用工具、选择哪个工具以及是否使用 `web_search` 继续由模型自主判断。
- 保留运行级工具目录快照和后台刷新，目录变化不阻塞已经取得快照的用户请求。
- 删除向量模型、向量索引、RRF、向量缓存及其配置和部署负担。

## 非目标

- 不改变 Planner 的任务 DAG 协议和重新规划策略。
- 不改变 Tool Agent 的参数生成、工具执行、超时、审批和错误恢复逻辑。
- 不把 `web_search` 改成代码自动兜底；它仍作为候选能力交给模型判断。
- 不在本次改动中引入业务同义词库、外部搜索服务或新的重排模型。

## 方案比较

### 方案一：保留混合检索并继续优化缓存

该方案在缓存命中时延迟较低，也能缓解中文同义表达造成的漏召回。但全新环境、缓存丢失和模型升级仍需加载模型并生成向量，且需要维护缓存格式、失效规则、模型 revision 和异常降级。复杂度与当前数百个结构化工具的规模不匹配。

### 方案二：只保留 BM25，在模型侧完成语义判断

该方案启动成本低，工具目录更新只需重建内存 BM25 索引。检索使用规范化的工具文档和 Planner 生成的能力查询缩小候选集，模型负责最终选择。它牺牲一部分纯语义召回，但更符合当前工具规模和动态 MCP 目录特征。

### 方案三：不做检索，向模型披露全部工具

该方案实现最简单，但工具数量增长后会显著增加上下文和工具 schema 体积，也会降低模型选取稳定性。权限和 Skill 作用域仍需要代码过滤，因此无法真正删除候选发现层。

采用方案二。Skill 由于候选量少且已有业务作用域，采用比工具更简单的“作用域披露 + 模型选择”。

## 架构与数据流

### Skill 发现

`SkillRegistry.discover()` 从 Admin 读取启用的 Skill 卡片，并按 `domain`、`subdomain` 过滤。Planner 将所有符合条件的 `name`、`summary` 卡片写入提示词，由模型决定任务是否引用某个 Skill。

删除 `SkillSearchService`、`skill_search_top_k` 和 Skill BM25 测试。候选顺序按 Skill 名称稳定排序，避免 Admin 返回顺序变化导致提示词漂移。完整 Skill 定义仍仅在 Planner 选中后加载。

### 工具发现

工具目录快照继续包含当前 `ToolRegistry` 与对应 `ToolSearchService`。构建搜索服务时，`ToolSearchIndex` 只为规范化工具文档建立 BM25 索引，不加载模型，也不生成或读取向量。

每次搜索按以下顺序执行：

1. 根据权限范围、Skill `allowed_tools` 和平台工具排除规则得到可见工具集合。
2. 查询中明确包含规范化工具名时返回精确匹配。
3. 否则使用 BM25 召回 `candidate_k` 个候选。
4. 使用词元覆盖率和参数 schema 覆盖率进行确定性轻量重排，返回 Top-K。
5. `ToolCandidateResolver` 接受精确匹配，或接受满足最小词元覆盖率的 BM25 命中。
6. Tool Agent 在专业候选和允许的 `web_search` 中自主决定是否调用以及调用哪个工具。

BM25 异常时抛出 `ToolSearchUnavailableError`。由于不再存在第二条检索通道，响应不再表达 hybrid、dense fallback 或 degraded 状态。

### 工具目录刷新

保留 `ToolCatalogRuntime.snapshot_pair_for_run()` 的运行级一致性和 stale-while-revalidate 行为。已有快照过期时，当前 Run 继续使用同一对 registry 和 search service，后台线程刷新下一份快照。进程首次启动且没有快照时仍需同步发现 MCP 工具并建立 BM25 索引，但不再承担向量模型冷启动。

## 接口收敛

`ToolSearchIndex.build()` 删除 `embedder` 参数。`ToolSearchHit` 删除 `fusion_score` 和 `dense_score`，保留 BM25、词元覆盖、schema 覆盖和综合相关性分数用于候选判断与追踪。`SearchSource` 收敛为 `exact | bm25`，`SearchMode` 收敛为 `exact | bm25`。

旧 checkpoint 或持久化 trace 中多出的向量字段由 Pydantic 默认的额外字段忽略行为兼容读取。新输出不再生成这些字段，从而减少传给模型的无效信息。

## 依赖与部署

删除 `fastembed` 直接依赖、FastEmbed 模型预下载步骤和 `BS_AGENT_TOOL_SEARCH_EMBEDDING_*` 配置。若 `numpy` 不再被项目代码直接使用，则从项目直接依赖中删除；`bm25s` 自身所需的传递依赖继续由锁文件保留。

删除 `backend/app/tools/search/embedding.py` 及其缓存测试。此前的工具向量缓存设计文档标记为被本设计取代，避免后续维护者误认为向量缓存仍是当前架构。

## 测试

- 单元测试验证工具名精确匹配、BM25 排序、权限与 Skill 作用域过滤、无词元命中和 BM25 异常。
- 单元测试验证候选解析不再依赖 dense score，并继续把 `web_search` 作为模型可选候选。
- Planner 测试验证所有作用域内 Skill 卡片均被披露，作用域外卡片被排除，顺序稳定。
- 目录运行时测试继续验证后台刷新期间同一个 Run 获得一致的 registry/search 快照。
- 部署测试验证镜像不再下载或配置 FastEmbed 模型。
- 运行后端单元、集成和部署相关测试，并执行 Ruff 与 `git diff --check`。

## 验收标准

- 服务启动和工具目录刷新不加载任何 embedding 模型，也不读写工具向量缓存。
- 工具搜索响应只出现 `exact` 或 `bm25` 模式及来源。
- 专业工具候选仍受权限、Skill `allowed_tools` 和检索预算约束。
- `web_search` 的调用时机仍由模型根据工具说明和系统指令决定。
- Planner 能看到全部作用域内 Skill 卡片，并只能引用已披露 Skill。
- 相关测试、静态检查和格式检查全部通过。
