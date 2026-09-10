# Tool Agent、Function Calling 与 MCP

## 定位

Tool Agent 执行 Planner 已选定的单个工具子任务。它不拆分任务、不自由选择其他工具、不把业务失败伪装成成功数据。

完整链路为：

```text
MCP Streamable HTTP list_tools
-> ToolRegistry 冻结 ToolSpec 与 ExecutionBinding
-> ToolSearchIndex 建立 BM25S + Dense 索引
-> Planner 获得 Top-K 轻量工具卡片
-> Planner 选择 tool_name 和 task_goal
-> Tool Agent 只绑定该工具完整 Schema
-> LLM 返回原生 AIMessage.tool_calls
-> Tool Gateway 执行治理校验
-> Provider/MCP 执行
-> ToolMessage + TaskResult
-> Scheduler 推进依赖任务
```

## 启动期动态发现

生产市场数据直接连接 Tushare MCP Server，通过 LangChain MCP Adapter 的 Streamable HTTP 客户端执行 `list_tools`。项目内 `mcp server` 只作为学习和本地联调服务，不是生产数据链路的必经代理。

每个远端工具被适配为内部 `ToolSpec`，保存非秘密的 Provider、Server、Deployment 和 Config revision。目录冻结后生成 `catalog_digest`，同一次 Run 的 Planner、Tool Agent、Gateway 和审批恢复必须使用同一快照。

## 渐进式披露

应用启动时会加载所有可用工具到进程内 Registry，但不把全量 Schema 放入每次 Prompt。

1. 检索层先按 Principal Scope 过滤不可见工具。
2. BM25S 与 Dense 检索通过 RRF 融合，返回 Top-K 工具名、描述和必需参数名。
3. Planner 只能选择本轮已披露工具。
4. 只有选中工具进入 Tool Agent 后，完整输入 Schema 才绑定给 LLM 用于生成参数。
5. 如果首轮 Top-K 不足，Planner 可将内置 `search_tool` 作为一个普通子任务，扩充披露集合后重新规划。

该机制同时限制搜索次数、总披露工具数和 Planner 轮次，防止上下文膨胀与无界循环。

## 原生 Function Calling

Tool Agent 对本任务工具执行 `bind_tools(..., tool_choice=<name>)`，接受且仅接受一个原生 `AIMessage.tool_calls`。模型给出的 call ID 不被信任，系统用 `request_id + planner_iteration + task_id + tool_name` 生成稳定可信 ID。

执行结果写为 `ToolMessage`：

- `content` 只保留给后续 LLM 使用的脱敏紧凑 JSON。
- `artifact` 保留脱敏后的结构化结果，供观测与程序化处理。
- `tool_call_id` 必须与对应 AIMessage 中的可信 ID 一致。

## 执行治理

LLM 只能提供参数建议。Tool Gateway 在真正调用 Provider 前依次执行：

- 工具名、版本、目录摘要和 execution binding 校验。
- Pydantic/JSON Schema 参数验证，拒绝额外字段。
- 租户、用户和 permission scope 的 RBAC 校验。
- 拒绝内联凭证、`writeOnly/x-sensitive` 及应由 Server 注入的字段。
- 根据真实 `ToolSpec.risk_level` 判断是否需要 Interrupt 审批。
- 超时、Provider 结果归一化、敏感数据脱敏和 Trace 关联。

高风险调用在审批前将完整请求保存到 PostgresSaver。恢复后重用同一个 `call_id + arguments_hash`，不重新请求 LLM 生成参数。

## 失败分类

- 超时、限流、MCP/Provider 短暂不可用：抛出可重试异常，交给 LangGraph `RetryPolicy`。
- 参数不合法、必需数据缺失、接口权限未开通：转成 `business_failure` Observation，由 Planner 判断是否改用其他合法路径。
- 目录变化、RBAC 拒绝、审批拒绝和不可恢复契约错误：终止当前任务，不盲目重试。

RAG 已是普通的 `rag_search` Tool，由同一 Tool Agent/Gateway 路径执行，不再存在独立 Retriever 执行分支。
