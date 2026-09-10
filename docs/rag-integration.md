# RAG 微服务接入说明

## 目标

BsAgent 的生产问答路径默认通过同级 `rag` 项目提供的 RAG 微服务检索证据。测试和本地快速验证可以显式开启 mock evidence，但 mock 不再是 Session API 的默认生产行为。

## 服务位置

当前约定 RAG 微服务代码位于同级目录：

```text
/Users/myfolder/前端学习项目/rag
```

BsAgent 不直接读取该目录的数据文件，而是通过 HTTP 调用 RAG 服务。

## HTTP 契约

BsAgent 调用的检索接口是：

```text
POST /api/rag/retrieve
```

最小请求：

```json
{
  "query": "银之杰近期有哪些公开风险信息？"
}
```

BsAgent 当前发送的主要字段：

```json
{
  "query": "银之杰近期有哪些公开风险信息？",
  "filters": {},
  "retrieval": {
    "topK": 8,
    "contextLimit": 8,
    "hybrid": true,
    "rerank": true,
    "queryRewrite": true,
    "graphRag": false
  },
  "responseMode": {
    "includeText": true,
    "includeGraph": true,
    "includeScores": true,
    "includeDebug": false
  }
}
```

RAG 响应中的 `contexts` 会被归一化为 BsAgent 内部的 `EvidencePackage.items`。

## 配置项

环境变量使用 `BS_AGENT_` 前缀。

```text
BS_AGENT_RAG_BASE_URL=http://localhost:8001
BS_AGENT_RAG_TIMEOUT_SECONDS=30
BS_AGENT_USE_MOCK_RAG=false
BS_AGENT_DATABASE_URL=sqlite:///./bs_agent.db
BS_AGENT_REDIS_URL=redis://localhost:6379/0
BS_AGENT_SHORT_TERM_MEMORY_BACKEND=none
BS_AGENT_SHORT_TERM_MEMORY_TTL_SECONDS=3600
BS_AGENT_SHORT_TERM_MEMORY_MAX_EVENTS=100
```

- `BS_AGENT_RAG_BASE_URL`：RAG 微服务地址。
- `BS_AGENT_RAG_TIMEOUT_SECONDS`：BsAgent 调用 RAG 微服务的 HTTP 超时时间。真实服务首次加载 embedding/rerank 模型可能较慢，建议不低于 `30`。
- `BS_AGENT_USE_MOCK_RAG`：是否让 Session API 使用内置 mock evidence。默认 `false`。
- `BS_AGENT_DATABASE_URL`：Agent 运行审计数据落库地址。
- `BS_AGENT_REDIS_URL`：Redis 连接地址，保留给后续短期热数据、缓存、TTL 状态或限流能力使用。当前不作为长期 Experience Store 后端。
- `BS_AGENT_SHORT_TERM_MEMORY_BACKEND`：短期热数据后端。默认 `none`，可设置为 `redis`。
- `BS_AGENT_SHORT_TERM_MEMORY_TTL_SECONDS`：短期热数据默认 TTL。
- `BS_AGENT_SHORT_TERM_MEMORY_MAX_EVENTS`：每个会话保留的最近事件数量上限。

## 错误语义

- RAG 返回 `200` 且包含 `contexts`：映射为证据包，进入分析、生成、校验链路。
- RAG 返回 `404`：表示没有相关证据，映射为 `coverage="none"`，后续进入无证据/审批语义，不作为系统错误。
- RAG 返回 `503`：通常表示索引或本地依赖未就绪，映射为 `rag_index_unavailable`，Session API 返回结构化 `503`。
- RAG 返回 `502`：通常表示 RAG 内部下游模型、rerank 或图谱依赖异常，映射为 `rag_upstream_error`，Session API 返回结构化 `502`。
- RAG 返回其它 HTTP 错误、网络超时或响应 schema 不合法：视为图执行失败，Session API 返回结构化错误。
- RAG 返回空 `contexts`：映射为 `coverage="none"`。

## 本地测试模式

普通自动化测试不依赖真实 RAG 服务，而是在测试 Tool Registry 中注册可控的 `rag_search` Tool 替身。生产运行时没有 Mock RAG 配置分支。

真实联调时应保持：

```text
BS_AGENT_USE_MOCK_RAG=false
```

并确认 RAG 微服务已经启动且 `/api/rag/health` 可用。

## 真实联调测试

默认测试套件不会访问真实 RAG 服务。如果需要验证 BsAgent 和同级 `rag` 微服务的真实 HTTP 契约，可以先启动 RAG 服务，然后执行：

```bash
BS_AGENT_RUN_LIVE_RAG_TESTS=true \
BS_AGENT_RAG_BASE_URL=http://localhost:8001 \
python3 -m pytest backend/app/tests/integration/test_live_rag_service.py -v
```

这条测试会通过 `RagSearchTool` 调用 `/api/rag/retrieve`，并检查返回结果能被 BsAgent 归一化为内部检索结果。

live 契约测试默认使用轻量检索参数：关闭 rerank、query rewrite 和 graphRag，用于快速验证 HTTP 契约与证据归一化。完整生产检索链路可以在后续专门的质量评估中开启增强选项。
