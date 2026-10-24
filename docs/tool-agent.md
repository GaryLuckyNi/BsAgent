# Tool Agent、Function Calling 与 MCP

> 2026-10-24：新批次恢复默认关闭；来源政策已最小接入，专用 PostgreSQL 两项恢复并发测试已通过，另有五项已在修改前复现的历史数据库回归失败，详见[来源政策验收](./acceptance/2026-10-24-recovery-source-policy.md)。下文内存预算和单项恢复描述对应原兼容模式。

新模式的普通及替代搜索均经 Gateway 共享持久化六次预算，调用身份稳定；完整结果摘要可复用，running/unknown/不完整结果不重发。恢复不重跑工具选择模型，仍保留正常 Planner 证据评审。TaskResult success 表示本次执行观察，不证明原任务验收或原接口数据已满足。

同批中高风险调用被本地 running 保护挡住、且尚无远端回执时，等待队列每轮只放行一个，保留原参数，不重新调用工具选择模型。已执行并形成权限恢复事项的调用不再进入本地等待队列；实际 unknown 仍阻止后续受保护调用。三项竞争与完整批次确认回归见[等待派发修复记录](./acceptance/2026-10-24-deferred-tool-dispatch-fix.md)。settled 仅表示回执已结算，不等于工具成功、数据充分或应弹权限确认。

### 新模式来源约束的最小接入

`BS_AGENT_PERMISSION_RECOVERY_SOURCE_POLICY` 默认 `unknown`；显式配置为 `public_supplement_only` 后，业务仅允许询问用户是否补充公开资料，并不自动执行搜索，也不承认原接口等价替代。新 Run 冻结版本化配置，恢复前与当前配置重新比较；配置放宽不能升级旧 Run，撤回会拒绝旧批次。总开关仍默认关闭，配置示例不代表完成生产启用验收。

既有 Intent Router 同一次调用提取 `source_constraint`（`unrestricted/original_only/unclear`），既有 Planner 同一次调用提取任务 `source_requirement`（`public_information/original_data/unclear`），不增加模型调用。局部来源限制保守收紧整个请求。只有服务端契约允许、请求不限来源、任务可用公开资料且不绑定固定 Skill，才可能进入原有批次确认；缺字段默认 unclear，模型不能通过输出 allowed 授权搜索。

原始序列/结构化数据计算、指定来源、固定流程和未知约束均不走替代搜索，返回业务缺口，不因此抛权限异常结束整个 Run。可独立执行的健康任务继续；依赖缺失数据的任务不能冒充完成。自然语言约束识别仍依赖既有模型，不保证任意自由文本绝不误解；强制来源限制需要结构化请求契约，当前未增加这类新 UI 或后台系统。

固定 Skill 验收查找冻结工具/版本的成功且执行确定的原始调用，再检查必需字段；不能把另一条搜索文章当成原始序列。JSON schema 只证明其实际可执行的字段、类型、数组长度等条件，不证明模型生成数字真实，也不自动验证任意自然语言期间要求。

## 定位

Tool Agent 执行 planner 给出的信息目标，不负责上层任务拆分。模型根据任务、已有材料和检索候选，自主决定是否调用工具以及调用哪个工具。

```text
MCP list_tools → 冻结 ToolRegistry 与执行绑定
→ 按任务检索专业候选，独立披露有权限的 web_search
→ 模型自主选择一次调用或不调用
→ 参数校验、审批、预算、执行网关
→ ToolMessage 与任务观察
→ planner 自主决定继续、修订或结束
```

## 启动期动态发现

生产市场数据直接连接 Tushare MCP Server，通过 LangChain MCP Adapter 的 Streamable HTTP 客户端执行 `list_tools`。项目内 `mcp server` 只作为学习和本地联调服务，不是生产数据链路的必经代理。

每个远端工具被适配为内部 `ToolSpec`，保存非秘密的 Provider、Server、Deployment 和 Config revision。目录冻结后生成 `catalog_digest`，同一次 Run 的 Planner、Tool Agent、Gateway 和审批恢复必须使用同一快照。

生产环境在启动时加载工具目录，本地 `npm run dev` 也在启动时预热目录。目录过期后，已有快照可供新请求继续使用；刷新在后台执行，请求同时取得配对的工具清单和检索索引。正在运行的请求保留原快照，刷新不会提前关闭旧版检索服务。首次尚无快照时仍须等待目录构建完成。

每份目录快照只为工具名、描述、参数名和参数说明建立内存 BM25 索引。目录变化时后台重建轻量索引，不加载向量模型，不生成或读取文档向量，也不需要模型缓存。运行快照仍使用完整 `catalog_digest` 校验检索结果与执行绑定；MCP 发现和实际业务工具调用不属于索引构建过程。

## 渐进式披露

应用启动时会加载所有可用工具到进程内 Registry，但不把全量 Schema 放入每次 Prompt。

1. 检索层先按 Principal Scope 过滤不可见工具。
2. 工具名明确出现时使用精确匹配，否则由 BM25 返回 Top-K 工具名、描述和必需参数名。
3. Planner 不选择普通工具，tool_search_query 是可选的能力检索提示。
4. 将合格候选完整 Schema 提供给 Tool Agent；web_search 独立于专业候选命中和排序，在授权范围内披露。
5. 检索只负责候选发现，不按命中或排名设置必须调用，不自动执行搜索。当前未增加额外的模型驱动 search_tool 循环；模型需要不同路径时可通过反馈修订任务目标。

候选数量、任务数量、反馈次数和搜索执行次数分别有预算。候选不足时仍允许模型基于材料回答并说明缺口，不强制搜索。

## 搜索使用时机

工具使用指令要求模型根据公开证据缺口、时效性、用户明确搜索请求和已有材料决定是否搜索。专业工具命中时仍可选择 web_search，未命中时也可以不调用。代码负责权限、参数和资源校验，不决定业务调用顺序。

搜索配额在模型提出真实调用后，在同一图实例内以锁原子预留，最多六个不同调用；不会按任务位置提前分配。恢复时从已记录搜索调用重建使用量。该计数不提供跨进程分布式租约，也不保证进程崩溃前未落盘调用的精确计量；服务仍须保持每个 Run 单一执行者。

## 原生 Function Calling

Tool Agent 对候选执行 `bind_tools(..., tool_choice="auto")`，允许零或一个原生工具调用；模型不调用时保留回答或缺口说明。若模型一次返回多个调用或未知工具，先将契约错误反馈给模型修复一次，保持 `auto` 选择权；再次失败才记录业务失败观察。参数修复针对模型已选定的调用，允许一次保持工具不变的修复。模型给出的 call ID 不被信任，系统用 `request_id + planner_iteration + task_id + tool_name` 生成稳定可信 ID。

Planner 生成工具子任务时必须保证它代表一次可执行调用，并能从用户消息或依赖任务结果中确定必需参数。对于“比较多家公司”这类复数目标，应先拆分实体或为每个实体建立独立任务，不能把复数抽象描述直接传给单实体工具。

参数生成上下文包含当前 `task_goal`、`completion_criteria`、直接依赖结果、显式引用的历史成功结果，以及一份有长度限制的用户材料（当前问题、历史与附件），不再另附完整会话副本。其他并行分支产生的内部 `AIMessage.tool_calls`、`ToolMessage` 和 Skill/Analysis 过程消息会被过滤，防止不相关工具名与参数污染当前节点。

DeepSeek 参数生成通过 LangChain 同步 `invoke()` 执行，并显式使用非 thinking 模式。该步负责候选选择与参数生成；完成情况由后续 planner 结合任务验收条件与结果判断。

执行结果写为 `ToolMessage`：

- `content` 只保留给后续 LLM 使用的脱敏紧凑 JSON。
- `artifact` 保留脱敏后的结构化结果，供观测与程序化处理。
- `tool_call_id` 必须与对应 AIMessage 中的可信 ID 一致。

工具结果进入任务观察前按 8,000 字符契约压缩。联网搜索优先保留回答、来源 URL 和简短摘要；其他超长结果保留脱敏预览。完整的结构化工具结果仍在 `ToolCallRecord` 中，超长正文不会因任务观察字段限制使节点失败。

## 执行治理

LLM 只能提供参数建议。Tool Gateway 在真正调用 Provider 前依次执行：

- 工具名、版本、目录摘要和 execution binding 校验。
- Pydantic/JSON Schema 参数验证，拒绝额外字段。
- 租户、用户和 permission scope 的 RBAC 校验。
- 拒绝内联凭证、`writeOnly/x-sensitive` 及应由 Server 注入的字段。
- 根据真实 `ToolSpec.risk_level` 判断是否需要 Interrupt 审批。
- 超时、Provider 结果归一化、敏感数据脱敏和 Trace 关联。

高风险调用在审批前将完整请求保存到 PostgresSaver。恢复后重用同一个 `call_id + arguments_hash`，不重新请求 LLM 生成参数。

## 失败与反馈

- Provider 超时、限流及业务失败均保留规范化结果，不再抛异常让整个 Tool Agent 重跑模型选择。
- MCP 通用执行失败仅记录安全分类 `remote_tool_error`、`response_type_invalid` 或 `client_exception`；客户端异常另记录异常类名。远端原始错误正文、URL 和凭证不写入 ToolError 或 Trace。
- 参数不合法允许一次本地模型修复，失败后记录观察。
- auto 模式没有 Tool Call 是合法选择；未知工具或多个调用先修复一次，仍不合约才形成可诊断观察。
- 目录、RBAC 与审批约束继续生效，模型不能用重新规划绕过授权。
- ToolCallResult 的 success 表示调用完成，不自动证明研究目标充分；planner 在批次反馈中判断后续动作。

## 搜索研究目标与覆盖

`web_search` 通过已有 `run_with_context` 接口收到服务端绑定的 `SearchResearchContext`：当前任务目标、验收条件和带时区的 `as_of`。这些字段不是模型生成的工具参数，不改变参数哈希或审批快照；权限回退也只在用户确认后绑定并执行。建议 query 即使偏向其他方向，搜索模型仍收到原任务契约；盘中时点不得把尚未产生的收盘数据当作当前应取得的事实。续轮保留该上下文。

搜索响应的 `coverage` 独立于调用 status，包含 `complete/partial/missing/unknown`、已覆盖/未覆盖条件和判断依据。条件索引无效、缺少结构化覆盖声明或没有验收条件时为 `unknown`，不自动重试或宣称目标完成。它仍是搜索模型对同一批材料的自述，不能替代 Planner 的证据评审，也不能独立证明事实正确。

本次为工具输出契约增加可选覆盖字段，目录摘要会变化；旧 Run 如被现有目录一致性保护拒绝恢复，应重新发起请求，不绕过保护继续旧调用。不需要业务数据库迁移。

RAG 继续作为普通 rag_search 工具，MCP 连接和业务结果适配未在本轮重写。
