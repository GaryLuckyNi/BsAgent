# Langfuse Cloud 观测说明

## 1. 定位与边界

Langfuse Cloud 是 BsAgent 的 LLM/Agent 分析面，用于查看 LangGraph 层级、模型调用、时延、Token、成本和会话聚合。它不替代项目已有的 PostgreSQL 业务观测表。

| 能力 | 事实源 |
| --- | --- |
| AgentRun 状态、审批恢复、审计和受控排障 | PostgreSQL `agent_runs`、`agent_trace_events`、`agent_trace_artifacts` |
| Agent/Graph 调用树、Generation 时延、Token、成本和会话分析 | Langfuse Cloud |
| HTTP、数据库和进程基础设施遥测 | 现有 OpenTelemetry 后端 |

本阶段只验证 Langfuse Cloud Japan，不启用 Prompt Management、Dataset、Evaluation、告警或生产流量。

## 2. 本地配置

在 Langfuse Cloud Japan 创建项目和 API keys，然后在仓库根目录执行：

```bash
cp .env.example .env.local
```

填写以下字段：

```dotenv
BS_AGENT_LANGFUSE_ENABLED=true
BS_AGENT_LANGFUSE_PUBLIC_KEY=pk-lf-替换为Cloud项目PublicKey
BS_AGENT_LANGFUSE_SECRET_KEY=sk-lf-替换为Cloud项目SecretKey
```

默认 endpoint 是 `https://jp.cloud.langfuse.com`。只有需要显式覆盖时才增加：

```dotenv
BS_AGENT_LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com
```

`BS_AGENT_APP_ENV` 直接映射为 Langfuse environment，本地默认是 `local`。本地采样率默认是 1.0；如需降低验证数据量，可设置 `BS_AGENT_LANGFUSE_SAMPLE_RATE=0.2`。

`.env.local` 只用于本地开发，已被 Git 忽略。shell 中已有变量优先于文件内容。生产环境必须使用部署平台的 Secret Manager、Kubernetes Secret 或等价能力，不能复制本地文件。

## 3. 启动与最小验证

确保 DeepSeek key 已在 shell 或 `.env.local` 中配置，然后启动后端：

```bash
npm run dev:backend
```

从现有前端或 API 发起一条短的 Agent 请求。请求完成后进入 Langfuse Cloud Japan 的 Traces 页面，按 root observation metadata 中的 `business_trace_id` 查找。

一次合格的最小验证应满足：

1. 一个 AgentRun 对应一个 `bs-agent.run` 根 observation。
2. 根节点下能看到 LangGraph/LLM 子 observation。
3. Generation 显示精确模型名、耗时以及 `input`、`output`、`total` Token。
4. session ID 是 64 位摘要，不是原始 session key。
5. 普通执行标记为 `execution_segment=initial`；审批恢复标记为 `execution_segment=resume`，并复用同一业务 trace ID 生成的确定性 Langfuse trace ID。
6. 所有 observation 的 input/output 中都没有 prompt、用户问题、回答正文或 reasoning。
7. Cloud 暂时不可达时，本地 AgentRun 仍能完成，PostgreSQL 业务 Trace 仍可查询。

SDK 使用后台批处理。关闭应用时先执行 5 秒超时的 force flush，再调用官方 shutdown；业务请求不会等待 Cloud 成功写入。Langfuse 不属于 `/health/ready` 的依赖。

## 4. 数据安全边界

允许上传的字段：

- observation 名称和类型；
- 业务 trace ID；
- 摘要化 tenant/session 标识；
- `initial` 或 `resume` 执行段；
- 模型名和非敏感模型参数，例如 temperature、max tokens、thinking 开关；
- input/output/total Token、耗时、状态和稳定错误码；
- LangGraph 节点名和工具名。

禁止上传的字段：

- system prompt、用户问题和历史消息；
- 回答正文和模型 reasoning；
- 工具参数、工具结果、RAG 原文和图片内容；
- Authorization、Cookie、API key、MCP URL 中的 token；
- 原始异常文本、请求头和响应体；
- 原始 tenant ID、user ID 和 session key。

项目使用 Langfuse 官方 `mask_otel_spans` 在导出前删除最终 span 中的 input、output、prompt、completion、message 和 reasoning 内容属性。直连 DeepSeek HTTP 的 Generation 从创建时就不设置 input/output，只写模型、参数和 usage。该双层约束用于防止官方 LangChain callback 或第三方 instrumentation 自动捕获正文。

## 5. Token 与成本

直连 DeepSeek HTTP 返回的 OpenAI 兼容 usage 按以下规则上传：

| Provider 字段 | Langfuse usage |
| --- | --- |
| `prompt_tokens` | `input` |
| `completion_tokens` | `output` |
| `total_tokens` | `total` |

项目不上传 provider 的未知 usage 字段，避免把重叠计数误当成独立计费桶。项目也不硬编码价格。

如果 Generation 有 Token 但成本为空，在 Langfuse Project Settings > Models 中增加与运行时模型名完全匹配的模型定义，例如 `deepseek-v4-flash`，并配置 `input`、`output` 的价格。随后再发一条请求验证成本推断。

## 6. 停用与排障

停用只需修改并重启后端：

```dotenv
BS_AGENT_LANGFUSE_ENABLED=false
```

关闭后不会初始化 Langfuse client，不会增加 callback，也不会改变 PostgreSQL 业务 Trace、现有 OpenTelemetry 或 Agent 返回值。

常见问题：

- 启动时报缺少 key：启用状态必须同时配置 public key 和 secret key。
- 启动时报 base URL：启用状态只接受 HTTPS endpoint。
- Cloud 中暂时看不到 trace：先等待后台 flush，再检查 Japan 项目和 key 是否匹配；不要在日志中打印 key 排查。
- 有 Token 但没有成本：在 Langfuse 模型设置中补充精确模型定义。
- 发现 input/output/reasoning 内容：立即关闭 `BS_AGENT_LANGFUSE_ENABLED`，保留对应 business trace ID，检查导出前 masking 测试和实际 observation 属性后再恢复。
