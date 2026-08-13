# Tool Agent、Function Calling 与 MCP Gateway 设计

## 目标

Tool Agent 是 BsAgent 的工具调用边界。它把模型、Skill 和外部系统隔开，确保所有原子能力都经过确定性的 schema 校验、版本检查、审批闸门、执行归一化和 trace 绑定。

第一版先实现本地可测试的最小闭环：

```text
Skill / LLM function call
-> ToolCallRequest
-> ToolRegistry
-> ToolExecutionContext
-> ToolExecutionGateway
-> Tool.run(...)
-> ToolCallResult
```

现有 RAG 微服务仍然是 retrieval-only 工具，通过 `rag_search` 注册为普通 Tool。Retriever Agent 负责检索策略，Tool Agent 只负责可靠执行。

## 模块边界

### `skills/definitions.py`

定义 Skill Registry Server 返回的结构化 JSON 模型：

- `SkillCard`：轻量摘要 JSON，用于 intent/skill 初筛，只包含名称、版本、摘要、支持意图、标签、风险等级和 definition hash。
- `SkillDefinition`：选中 skill 后再加载的完整结构化定义 JSON，包含输入契约、输出契约、允许工具、约束和失败模式。
- `SkillExecutionGuide`：执行前再加载，包含执行指令、few-shot 示例、恢复策略和证据要求。

BsAgent 不要求 skill 定义内置在项目里。推荐把 Skill Registry Server 作为单一事实源，BsAgent 只消费、缓存和校验结构化 JSON。

### `skills/registry_client.py`

定义 `SkillDefinitionProvider` 协议和两个消费侧实现：

- `RemoteSkillRegistryClient`：通过 HTTP 从远端 Skill Registry 拉取 cards/definition/execution-guide。
- `CachedSkillDefinitionProvider`：在 BsAgent 进程内做 TTL 缓存，避免每次请求都访问 registry。

当前约定的远端接口：

```text
GET /skills/cards?tenant_id=...
GET /skills/{name}/versions/{version}/definition
GET /skills/{name}/versions/{version}/execution-guide
```

配置项：

```text
BS_AGENT_SKILL_REGISTRY_BASE_URL=http://localhost:8010
BS_AGENT_SKILL_DEFINITION_CACHE_TTL_SECONDS=300
```

### `skills/registry.py`

`SkillRegistry` 保留本地 skill 注册和选择能力，同时新增远端 definition 发现能力：

- `discover(tenant_id, intent)`：只返回 `SkillCard`，用于渐进式初筛。
- `get_definition_layers(name, version)`：选中后再加载 `SkillDefinition` 和 `SkillExecutionGuide`。

这让运行链路可以先只把轻量 skill card 放进上下文窗口，避免一次性展开所有 skill 的长说明。

### `graph/nodes/skill_agent.py`

`Skill Agent` 负责把 intent 决策转成可执行的 skill runtime plan：

```text
IntentDecision
-> SkillRegistry.select(...)
-> Skill.run_validated(...)
-> SkillOutput
-> state.selected_skill
-> state.plan.requested_tools
```

当前默认注册 `PolicyQaSkill`，也支持测试或后续运行时注入其它 `SkillRegistry`。`Skill.run_validated(...)` 会校验 skill 请求的工具必须在 `allowed_tools` 内，避免 skill 越权调用未授权工具。

### `graph/nodes/retriever_agent.py`

`Retriever Agent` 会优先执行 `state.plan.requested_tools` 中的 `rag_search` 请求，并通过 `ToolAgentExecutor` 统一执行：

```text
SkillOutput.requested_tools
-> ToolAgentExecutor.execute(...)
-> ToolCallRecord
-> EvidencePackage
```

没有 skill tool request 时，Retriever Agent 保留默认 `rag_search` 请求作为兼容路径。RAG 的 `404` 映射为正常无证据结果；已经消费 ready call 的 Provider 失败会提交为 `execution_error_response`，执行前的 ready/call ID/指纹契约错误才由 graph harness 终止运行。

Graph 主链中的 `rag_search` 在进入 Retriever 前也必须先经过 Tool Agent 的可信准备阶段：生成 `call_id`、解析当前参数、读取运行时 ToolSpec 并按真实风险决定是否审批。Tool Agent 不调用 RAG Provider，实际检索仍由 Retriever 执行；这样既保留 EvidencePackage 转换职责，也不会让高风险 RAG 绕过 Approval Gate。

### `schemas/tools.py`

定义系统级工具契约：

- `ToolSpec`：工具 manifest，包含名称、版本、输入输出 schema、超时、权限范围和风险等级。
- `RetryPolicy`：工具重试策略占位，当前只记录配置，不在第一版自动重试。
- `ToolCallRequest`：内部标准工具调用请求。
- `ToolCallResult`：内部标准工具调用结果。
- `ToolError`：结构化工具错误。

这些 schema 是 Skill、Function Calling、MCP Gateway 和审计系统之间的共享协议。

### `tools/base.py`

定义 `Tool` 抽象。每个 Tool 必须声明：

- `name`
- `version`
- `input_model`

`Tool.spec` 会自动从 `input_model.model_json_schema()` 生成输入 schema，避免工具契约和 Pydantic 参数模型漂移。

### `tools/registry.py`

`ToolRegistry` 是工具发现层。注册时会把完整 `ToolSpec` 和非秘密 `ExecutionBinding` 规范化序列化到不可变 `RegistryEntry`，executor 只保留为运行引用；因此构图后修改 executor 的风险、权限、schema 或实现身份不会改变冻结策略。外部 MCP/RAG/Adapter 注册必须显式提供 provider、server、deployment revision 和 config revision；这些字段只表达身份，禁止放 command 参数、token 或连接凭证。MCP 的 command、args、cwd、timeout 和默认风险配置会规范化后计算不可逆 SHA-256 摘要并写入 config revision，配置变化会改变目录摘要，但原始进程参数不会进入 State、Trace 或 checkpoint。纯本地工具可用 module、qualified class 和 package/build revision 稳定派生。`snapshot()` 返回只读目录，负责：

- 注册 Tool。
- 按名称获取 Tool。
- 校验请求版本和实际工具版本是否一致。
- 输出当前可用工具的 `ToolSpec` 列表。
- 计算覆盖完整 `ToolSpec.model_dump()` 与 `execution_binding_digest` 的稳定 `tool_catalog_digest`，包括 timeout、retry policy、schema、风险、权限和服务端注入字段。

### `tools/execution_gateway.py`

`ToolExecutionGateway` 是所有非 RAG 工具的统一执行治理边界，当前处理：

- 未注册工具：返回 `tool_not_found`。
- 版本冲突：返回 `tool_version_mismatch`。
- 不在 Plan 白名单：返回 `tool_not_allowed_by_plan`。
- 不在已选择 Skill 白名单：返回 `tool_not_allowed_by_skill`。
- 缺少工具注册端声明的权限 scope：返回 `tool_permission_denied`。
- 参数先按注册时冻结的 JSON Schema 校验，再按 executor 的 Pydantic 模型解析；任一不符合均返回 `tool_argument_validation_failed`，无效输入契约返回 `tool_input_schema_invalid`。
- 请求标记需要审批或注册工具风险等级为 `high`，且当前 `call_id + 参数指纹` 未同时获批：返回 `tool_approval_required`，不执行工具。
- 工具运行异常：返回 `tool_execution_failed`。
- 成功结果不符合输出 schema：返回 `tool_output_validation_failed`，原始结果不进入 Agent State。
- 参数 `model_validate` 的 `ValidationError/TypeError` 等普通异常、Provider 异常、非法 dict/对象返回、`ToolCallResult` 校验错误和 JSON Schema 构造/校验异常都转换为稳定 error code 与白名单安全消息；`CancelledError`、`KeyboardInterrupt` 和 `SystemExit` 不会被吞掉。
- Provider 的合法 dict 返回会先经过 `ToolCallResult.model_validate`。无论成功或失败，`tool_call_id` 都由 Gateway 重建，`tool_name` 强制使用请求工具名，`trace_id` 强制使用当前 trace，latency 使用 Gateway 观测值；成功结果清空 Provider 附带的 error，失败结果清空 result 且 status/error 只保留受控枚举和白名单 code/message。Provider 植入这些控制字段的任意内容不会进入 State、Trace 或 checkpoint，Tool Agent 写 State 前还会二次归一化。

`ToolExecutionContext` 携带 tenant、user、session、Plan 白名单、Skill 白名单、当前权限 scope、已批准调用实例 ID 和参数哈希。模型和 Planner 只能提出调用建议，执行网关会在真正调用 Provider 前重新做确定性校验。

Tool Agent 与 Retriever 统一通过 `graph/tool_context.py` 构造执行上下文。Plan 白名单只来自 `plan.tool_policy.allowed_tools`；Skill 声明了 `selected_skill.allowed_tools` 时再叠加 Skill 白名单；permission scopes 与 approved hashes 均来自运行状态。Retriever 不得用当前请求名自授权。只有 Plan 未提供 `tool_policy.allowed_tools` 时，Retriever 才使用代码中命名的系统默认检索策略 `DEFAULT_RETRIEVAL_ALLOWED_TOOLS=("rag_search",)`；显式空白名单仍表示拒绝。

`ToolExecutionContext` 是强制安全边界。任何调用省略 context 时，Gateway 都会在工具发现和 Provider 调用前返回 `denied/tool_execution_context_required`；系统不存在自动补充 `permission_scopes=["*"]` 的兼容管理员上下文。

`tools/agent.py` 暂时保留 `ToolAgentExecutor` 兼容导入，主运行链路使用 `ToolExecutionGateway`。

这层不负责选择工具，也不负责检索策略。选择工具属于 Skill Agent / Planner / Retriever Agent；策略属于对应 Agent；Tool Agent 只做边界治理。

## Function Calling 适配

`tools/function_calling.py` 将模型返回的 function/tool call 转换为内部 `ToolCallRequest`。

支持两种常见形态：

```json
{
  "name": "rag_search",
  "arguments": "{\"query\":\"报销制度\"}"
}
```

```json
{
  "function": {
    "name": "rag_search",
    "arguments": {"query": "报销制度"}
  }
}
```

适配器只做解析，不直接执行工具。执行必须交给 `ToolExecutionGateway`。

## MCP Gateway 与动态发现

BsAgent 默认不再手工注册本地 MCP adapter。构建默认 `ToolRegistry` 时，`McpGateway` 通过官方 `langchain-mcp-adapters` 连接独立的 Streamable HTTP Server，并调用 `MultiServerMCPClient.get_tools()` 动态发现工具：

```text
MCP Streamable HTTP Server
-> MultiServerMCPClient.get_tools()
-> McpGateway.discover_tools()
-> DynamicMcpHttpTool
-> ToolRegistry
-> ToolSpec(name="mcp.<server>.<tool>")
```

命名规则：

```text
mcp.<server_name>.<tool_name>
```

权限范围规则：

```text
mcp:<server_name>
```

当前已经提供一个真实 MCP Streamable HTTP Server，用于验证 Agent 调用 MCP 工具：

```text
mcp server/
```

MCP Server 是独立进程，默认暴露标准 Streamable HTTP Endpoint：

```text
http://127.0.0.1:8011/mcp
```

BsAgent 默认配置：

```text
BS_AGENT_MCP_SERVER_URL=http://127.0.0.1:8011/mcp
BS_AGENT_MCP_SERVER_NAME=local
BS_AGENT_MCP_TIMEOUT_SECONDS=30
```

`tools/mcp_http.py` 使用官方 `MultiServerMCPClient` 获取 LangChain Tool，并包装为内部 `DynamicMcpHttpTool`。运行时调用官方 LangChain Tool 的 `ainvoke(...)`；结构化结果从 `ToolMessage.artifact.structured_content` 读取，再进入现有 Tool Agent 治理边界。

## 金融 MCP 工具

A 股结构化数据通过 MCP Streamable HTTP 动态发现为内部 `ToolSpec`。Agent 不直接访问财经 API，也不在 Agent 代码中内置数据源 SDK；结构化行情、公司资料、财务指标等能力都必须经过 MCP Server 和 Tool Agent 边界。

工具名由 MCP server 名称和远端工具名动态组成，不应在 Planner、Skill 或 Analysis 中写死 server 名称。能力示例：

- `mcp.<server>.stock.resolve_company`
- `mcp.<server>.stock.company_profile`
- `mcp.<server>.stock.market_snapshot`
- `mcp.<server>.stock.financial_metrics`
- `mcp.<server>.stock.announcement_search`

当前未提供结构化风险事件能力；风险扫描仅使用真实公告与 RAG 证据。

## 联网实体发现与市场复验

`web_search` 是平台级只读工具，不属于市场数据 MCP，也不由 Tushare Provider 实现。当前 Provider 使用 DeepSeek Anthropic API 的服务端 Web Search，启动时与 MCP 动态工具一起进入冻结的 `ToolRegistry`。

公司精确解析失败后的恢复链路为：

```text
stock.resolve_company 未命中
-> web_search 检索法定主体、上市主体、证券代码和公开披露
-> LLM 仅基于搜索结果 URL 输出单一实体关系
-> 确定性校验关系、置信度和来源 URL
-> 原 stock.resolve_company 复验候选名称或代码
-> 复验成功后才写入 $resolved_symbol/$resolved_name
```

允许进入复验的关系仅包括同一实体、正式别名、法定名称、明确上市主体、证券代码以及有来源支持的“上市主体之子公司”。`peer_company`、`related_company`、`supplier_customer` 等关系即使置信度很高也会被拒绝。搜索 URL 不在本次搜索结果中、关系不唯一、置信度不足或搜索代码与市场复验代码冲突时，Tool Agent 停止依赖调用并返回实体无法可靠确认。

市场目录中的名称相似候选不再用于自动修复，也不存在“唯一高相似候选直接尝试”的兜底。Web Search 是发现源，`stock.resolve_company` 的成功结果仍是证券身份进入行情、财务和公告工具的唯一入口。

`rag_search` 仍由 Retriever Agent 执行并映射为 `EvidencePackage`，但实际 Retriever executor 与非秘密 provider binding 会一起加入本次 Runtime Registry。Tool Agent 完成可信规范化、风险提升和审批准备后，把 Registry 中的 `execution_binding_digest` 写入 `ready_tool_call`；Retriever 复核 call ID、参数指纹和 binding digest 后，才通过自身冻结 Registry 中与 Runtime 条目绑定一致的实际 executor 执行。Runtime 不扫描计划中的工具名，前置失败不会误路由 Retriever，高风险 RAG 未获批也不会 ready，RAG provider revision 变化不能复用旧审批或 ready 状态。

RAG Provider 返回 502/503、超时、不可达或 Gateway 策略拒绝时，Retriever 不再抛异常使旧 checkpoint 保留 ready。它会记录失败 `ToolCallRecord`、清理 ready、追加安全结构化错误，再由 Synthesizer 生成 `execution_error_response`。该响应没有 claims/evidence，携带稳定 `business_error_code`，Verifier 不执行 citation 规则，ApprovalCoordinator 设置 `not_required`，也不会创建 `ApprovalRecord`。Session API 保留 HTTP 200，客户端必须检查 `response_type/business_error_code`。`rag_no_evidence` 仍是正常无覆盖结果；`rag_call_not_ready`、call ID 或指纹不匹配仍按执行契约错误终止 Graph。

依赖前置结果的工具使用 `$resolved_<output_key>` 占位协议，例如 resolver 返回 `symbol` 时，后续 `$resolved_symbol` 会被替换。解析逻辑不依赖工具名或证券代码字段：Tool Agent 按请求顺序汇总已成功工具的结构化输出，递归解析当前请求的占位值，再读取该工具的真实 `ToolSpec.risk_level`。安全前置工具可以先执行；真实参数需要审批时，Tool Agent 把解析后的请求写回 plan，并生成唯一 `pending_tool_approval` 后返回 Graph，绝不提前审批后续占位请求或调用高风险 Provider。批准恢复后，已成功请求按可信 `call_id` 跳过，不重复执行前置工具；指纹只用于审批防篡改，不用于合并两个有意的相同调用。

`McpGateway` 属于 MCP 控制面，只负责 MCP Server 配置、工具发现和协议标准化。它不负责通用权限与风险决策；这些职责属于 `ToolExecutionGateway`。

当前 MCP Gateway 已覆盖：

- 启动期 `list_tools` 动态发现和 Runtime Registry 快照。
- 输入/输出 Schema 校验、超时隔离和 MCP 错误归一化。
- 市场数据业务 envelope 校验和 `ToolCallResult` 转换。
- 与 `ToolExecutionGateway` 的 RBAC、风险、审批和凭证注入边界。
- Analysis 字段级事实引用，以及 Verifier 对 `tool_call_id + JSON path + value` 的回查。

## 审批与风险

工具调用是否需要人工审批由上游 Agent、Skill 和服务端工具风险共同决定。`ToolExecutionGateway` 不信任模型降低风险：即使 `ToolCallRequest.approval_required=false`，注册端 `ToolSpec.risk_level=high` 仍会强制返回 `denied`。

Tool Agent 在执行前使用本次运行实际 `ToolRegistry` 中的 `ToolSpec` 规范化请求。可信规范化层按 `run_id + step_index` 生成稳定 `call_id`，会覆盖模型提供的任意 ID。只要注册端 `risk_level=high`，就会把对应请求提升为 `approval_required=true`；若 `ApprovedToolCall(call_id, arguments_hash)` 中不存在当前调用的精确 pair，Tool Agent 不执行任何 Provider，而是让 Graph 路由到 Approval Gate。ID 和哈希不再分开授权，也不存在 hash-only 兼容放行。该判断不依赖工具名称枚举，也不信任 Skill 将风险标成 medium 或 low。

审批展示参数由 `tools/redaction.py` 集中生成，`ToolCallRequest` 不包含可由模型伪造的 `safe_arguments`。脱敏器对 snake/camel/kebab 键名分词后匹配敏感词组，并递归处理 Pydantic、dataclass、数组、集合和循环对象；JSON Schema 支持 `properties/items/prefixItems/additionalProperties/patternProperties/allOf/anyOf/oneOf`、本地 `$defs/definitions` 引用和循环引用保护。任一分支的 `writeOnly=true` 或 `x-sensitive=true` 都会整值脱敏，无法解析的本地安全引用或未知安全结构按整值 `***` fail-closed。Tool Agent 从 Registry 取得 schema 后生成安全展示副本并绑定 `call_id + fingerprint`，Approval Gate 和 Approval API 不返回完整秘密。`arguments_hash` 始终基于原始真实参数计算，不使用脱敏值。

连接凭证不得作为模型、Skill 或 MCP 参数进入 Agent State/checkpoint。URL query key 复用统一敏感分词规则，支持 URL 编码、大小写和重复参数，同时保留普通 URL、`token_count`、token economics 与 A 股 symbol 的反误报边界。Graph 启动时冻结一次 ToolRegistry 快照，Planner、Skill 出站 guard、Tool Agent 和 Gateway 共享该快照；目录安全契约摘要写入 State/checkpoint，Tool Agent、Approval Gate 和恢复入口发现变化时以 `tool_catalog_changed` fail closed。默认或自定义 Skill 的请求在节点返回 checkpoint 前都必须通过真实 ToolSpec 的 `server_injected_fields/writeOnly/x-sensitive` 校验，失败会恢复调用前 State。连接凭证只能由 Gateway/Adapter 服务端注入，不支持明文或假 `SecretRef`。当前 checkpoint 未加密，普通敏感业务参数仍需要加密 saver、字段加密和保留策略。

当前高风险工具审批链路已经形成闭环：

```text
Planner / Skill 生成 ToolCallRequest
-> Tool Agent 生成可信 call_id，执行安全前置步骤并只准备当前真实请求
-> Tool Agent 生成 pending descriptor（call_id + fingerprint + 安全展示参数）
-> Approval Gate 校验 descriptor 与真实请求后调用 interrupt()
-> ApprovalRecord 持久化 pending 业务审批
-> Approval API 原子记录 approved/rejected
-> Command(resume=...) 恢复 Run 级 checkpoint
-> Approval Gate 重新核对 approval_id、call_id 与参数哈希
-> ToolExecutionGateway 再次核对权限 scope 与已批准 call_id + 哈希
-> Tool Provider
```

`ApprovalRecord` 是业务审批审计记录；LangGraph checkpoint 保存节点执行现场并负责中断恢复；`AgentRun` 保存单次运行的业务状态与结果索引。三者相互关联但不可互相替代。审批 API 使用 `pending -> resuming -> approved/rejected|resume_failed` 状态机和数据库 CAS claim token：并发请求只有一个可以恢复，失败会记录错误并允许相同决定重试。相同终态决定是幂等请求，相反决定返回冲突。`approval_id` 绑定 `run_id + call_id + fingerprint`；工具名称、版本或参数变化会使旧审批失效，相同工具与参数的两个不同调用实例也各自审批、各自执行。

`decision/decided_by/decided_at` 是首次业务决定，写入后不可被恢复重试人覆盖；`resume_claimed_by/resume_claimed_at` 记录最近一次恢复 claimant，`resume_attempts` 记录总尝试数，`last_resume_error` 在新 claim 或最终成功时仍保留最近失败证据。TraceEvent 使用稳定 `event_id`，`persist_agent_run` 依据数据库记录幂等补写，不再使用进程内事件数量作为持久化游标。

当前 TraceStore 仍在进程内。业务收尾回滚后，只要原进程存活，相同决定重试可补齐 Trace；若进程在事件入库前退出，内存 Trace 无法仅靠已完成 checkpoint 跨进程恢复。本阶段不把该链路描述为持久观测或分布式 exactly-once。

Trace Artifact 使用固定白名单摘要，只记录数量、状态、证据 ID、工具名、call ID、耗时、错误码和正文长度；不记录用户消息、analysis、工具参数/结果、草稿与最终回答正文。NodeRunner 不保存 `str(exc)`，`event.error` 只使用稳定的 `error_code:ExceptionType`，metadata 只包含 error code、exception type 和服务端白名单 message。TraceStore 与 API 仍执行二次清洗，覆盖 URI userinfo/query、Basic/Bearer、JWT、冒号和等号赋值。原始诊断当前不落 Trace，未来只能进入权限更高的安全日志。该边界不改变面向用户的正常 `final_response`。

当前 `MockRbacProvider` 仅供本地开发和测试，通过固定配置模拟角色与工具 scope。它不是生产鉴权：不验证 JWT、签名、令牌有效期或真实租户归属，生产环境不得启用。生产权限必须来自经过验证的身份主体及企业 IAM、OPA 或 Casbin 等可信权限源，不能把请求体、模型输出或 Prompt 内容当作授权依据。

## 测试覆盖

当前测试覆盖：

- 注册工具成功执行。
- 未知工具拒绝。
- 工具版本冲突拒绝。
- 参数 schema 不合法拒绝。
- 需要审批的调用拒绝执行。
- Plan/Skill 白名单越权拒绝。
- Retriever Plan/Skill 白名单越权拒绝，默认 RAG 策略不做请求自授权。
- 注册端高风险工具强制审批。
- 多工具前置解析、真实参数审批和恢复后前置工具不重放。
- 连续高风险调用逐个审批，相同指纹的不同调用实例不合并。
- 审批参数键名/schema 脱敏，原始参数指纹保持不变。
- 通用内联凭证在 DTO 边界拒绝，Planner/Tool Agent 按 ToolSpec 拒绝服务端注入字段；Trace 摘要/API 不返回正文或明文秘密。
- 工具权限 scope 拒绝与放行。
- 审批参数哈希稳定性及参数变化拒绝。
- Graph 高风险工具中断、批准恢复、拒绝恢复与旧哈希拒绝。
- 审批记录跨租户不可见、重复决定幂等和冲突决定拒绝。
- 工具输出 schema 不合法拒绝。
- MCP Gateway 动态发现委托。
- function calling JSON 参数解析。
- function calling dict 参数解析。
- function calling 非法 JSON 拒绝。
- MCP manifest 转内部 `ToolSpec`。
- MCP Streamable HTTP 工具动态发现与官方 LangChain adapter。
- `tool_action` 图分支按 Planner 生成的 ToolCall 执行动态 MCP 工具。
