# LLM Gateway 与 DeepSeek 接入

## 职责边界

Graph 节点通过统一 LLM Client 访问模型，不直接拼接某个厂商的 HTTP 请求。该边界集中处理 Provider 配置、API key、超时、错误归一化、Token 用量、Trace 绑定和模型切换。

当前生产 Provider 默认且必须为 DeepSeek：

```text
BS_AGENT_LLM_PROVIDER=deepseek
BS_AGENT_DEEPSEEK_API_KEY=...
BS_AGENT_DEEPSEEK_BASE_URL=https://api.deepseek.com
BS_AGENT_DEEPSEEK_MODEL=deepseek-v4-flash
BS_AGENT_LLM_TIMEOUT_SECONDS=60
```

缺少有效 LLM 配置时显式失败，不允许以 `none` 或默认 RAG 路由静默降级。

默认等待模型响应 60 秒，可通过 `BS_AGENT_LLM_TIMEOUT_SECONDS` 调整，取值需大于 0 且不超过 300 秒。模型响应超时会被归一化为可重试的 `llm_timeout`，并由现有错误边界返回安全提示，不自动重放模型请求。

## Graph 接入点

- Intent Router：只根据用户输入、附件摘要和 Admin taxonomy 判断 `intent_type/domain/subdomain`，并生成不改变语义的 `goal`。
- Planner：根据 `goal`、Top-K Skill/Tool 轻量卡片和既有 Observation 生成动态任务 DAG。
- Tool Agent：只绑定当前任务选中的一个完整 Tool Schema，使用原生 Function Calling 生成参数。
- Skill/Analysis Agent：使用依赖任务的可验证结果完成局部分析，不生成最终回答。
- Synthesizer：使用 `goal + TaskResult + Evidence` 生成终稿 Markdown，不改写工具事实。

## Web Search

```text
BS_AGENT_WEB_SEARCH_ENABLED=true
BS_AGENT_WEB_SEARCH_PROVIDER=deepseek
BS_AGENT_WEB_SEARCH_TIMEOUT_SECONDS=30
```

Web Search 启用后以普通只读 Tool 加入启动期 Registry 和检索索引。Intent Router 不在代码中预判必须联网；Planner 根据问题时效性、已披露能力和业务失败 Observation 决定是否创建 Web Search 子任务。

搜索结果是外部证据，不是行情或财务事实源。公司名、上市主体和证券代码的关系必须有来源支持并经结构化工具复验；同行、供应商或概念相关公司不能当作同一实体。

Web Search 仍经过 Tool Gateway 的 RBAC、Schema、超时、错误归一化和 Trace 边界，需要 `web:search` scope。

## 连通性测试

默认自动化测试不访问真实 DeepSeek。最小连通性验证使用：

```bash
BS_AGENT_RUN_LIVE_DEEPSEEK_TESTS=1 \
BS_AGENT_DEEPSEEK_API_KEY=你的_key \
python3 -m pytest backend/app/tests/integration/test_live_deepseek_llm.py -v
```

该测试只验证 API key、Base URL、模型名、网络与基本响应解析，不代替完整 Agent 端到端验收。
