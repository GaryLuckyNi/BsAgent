# Langfuse Cloud 最小观测验证设计

日期：2026-09-24
状态：已确认设计，尚未实施

## 一、目标

本轮在不替换现有业务 Trace、LangGraph Checkpointer 和 OpenTelemetry 链路的前提下，将本地 BsAgent 的一条完整 Agent 运行发送到 Langfuse Cloud Japan，验证以下能力：

- LangGraph 执行能够显示为一条可阅读的 Trace；
- LLM 调用能够显示模型、耗时、Token 和成本；
- 多轮运行能够通过脱敏 Session ID 聚合；
- 云端数据不包含 Prompt、回答正文、reasoning、原始工具参数或结果；
- Langfuse 不可用时不影响业务回答和本地业务 Trace。

本轮使用 Langfuse 官方 SDK 和官方 LangChain/LangGraph 集成，不自行实现 Trace 存储、批处理、重试、采样、成本计算或可视化页面。

## 二、现有能力与职责边界

现有观测能力继续保留：

| 数据层 | 职责 |
| --- | --- |
| `agent_runs` | 一次业务执行的状态、租户、风险、版本和 Trace 身份 |
| `agent_trace_events` | 节点、规划、工具、证据与 Verifier 的安全业务摘要 |
| `agent_trace_artifacts` | 受权限保护、经过脱敏的诊断产物 |
| LangGraph Checkpointer | 中断、审批和断点恢复现场 |
| OpenTelemetry | API、HTTP、数据库、Celery 与 Agent Node 的技术链路 |
| Langfuse Cloud | Agent/LLM Trace 页面、Session、Token、成本和后续评估入口 |

Langfuse 是新增的观测投影，不是业务事实源。业务数据仍以现有 PostgreSQL 表为准，恢复仍以 Checkpointer 为准。两边通过业务 `trace_id` 和 `run_id` 关联。

## 三、方案选择

### 3.1 采用方案：官方 SDK 与官方 Callback

采用以下官方能力：

- `langfuse` Python SDK；
- `langfuse.langchain.CallbackHandler` 接入 LangGraph；
- `LangfuseSpanProcessor` 与现有 OpenTelemetry 配置共存；
- `mask_otel_spans` 在导出阶段删除敏感属性；
- 官方 Generation Observation 记录绕过 LangChain 的直接 DeepSeek 调用；
- SDK 自带的批处理、重试、采样、flush、Session、usage 和成本能力。

项目只维护必要的接线代码：配置读取、生命周期初始化、业务标识映射、安全字段映射，以及把官方 Callback 传给 Graph。

不新增自研 `LangfuseObserver`，也不复制 Langfuse 的 Trace、Span、队列或成本模型。

### 3.2 未采用方案

| 方案 | 未采用原因 |
| --- | --- |
| OTel Collector 全量转发 | 当前自动采集包含 FastAPI、HTTPX 和 SQLAlchemy Span，存在 URL、SQL 等字段出域风险；现有 Span 也缺少完整的 LLM usage 语义 |
| 只使用 LangChain Callback | 项目中存在直接调用 DeepSeek HTTP API 的路径，单独使用 Callback 会漏掉部分 Generation |
| 自研 Langfuse 适配协议 | 会重复实现官方 SDK 已提供的上下文、批处理、重试和导出能力 |

## 四、部署与数据流

Langfuse 使用云端 Japan 区域：

```text
https://jp.cloud.langfuse.com
```

本地 Backend 通过 HTTPS 主动出站，不需要公网 IP、本地 Langfuse 服务或入站端口。

```text
请求进入
   │
   ▼
AgentRun 创建
   │
   ├── 业务 Trace ───────────────> PostgreSQL
   ├── 恢复状态 ─────────────────> LangGraph Checkpointer
   └── 官方 Langfuse SDK
         ├── LangGraph Callback
         ├── Generation Observation
         ├── 导出前 masking
         └── HTTPS ───────────────> Langfuse Cloud Japan
```

每个 `AgentRun` 对应一条 Langfuse Trace。Graph、LLM 和 Tool Observation 在官方上下文中形成父子关系。审批恢复沿用业务 `trace_id`，并使用 `execution_segment` 区分首次执行与恢复执行。

## 五、配置管理

### 5.1 本地开发

仓库提交 `.env.example`，只包含变量名和安全默认值；本机使用不提交的 `.env.local` 保存真实密钥。`npm run dev` 自动读取 `.env.local`，避免每次手工执行 `export`。

MVP 只要求三个本地变量：

```env
BS_AGENT_LANGFUSE_ENABLED=true
BS_AGENT_LANGFUSE_PUBLIC_KEY=pk-lf-...
BS_AGENT_LANGFUSE_SECRET_KEY=sk-lf-...
```

以下值使用项目默认配置：

- Host：`https://jp.cloud.langfuse.com`；
- Environment：由 `BS_AGENT_APP_ENV` 派生，本地为 `local`；
- MVP 采样率：`1.0`。

保留显式 `ENABLED` 开关，不能根据密钥是否存在自动启用数据出域。

### 5.2 生产环境

生产密钥通过 Secret Manager、Kubernetes Secret 或 CI/CD Secret 注入。真实密钥不得进入 Git、镜像、Compose 文件、日志或测试快照。普通非敏感配置可以继续使用部署环境变量或 ConfigMap。

本轮只完成开发环境 MVP，不启用生产上报。

## 六、上传数据契约

所有上传字段采用白名单。禁止把完整 State、Message、模型响应对象或异常对象交给 Langfuse。

| Observation | 允许字段 | 禁止字段 |
| --- | --- | --- |
| Agent Trace | Run 状态、Graph 版本、环境、脱敏 Session ID、业务 `trace_id/run_id` | 用户问题、最终回答正文、原始租户和用户标识 |
| Graph/Agent Span | 节点名、状态、耗时、规划轮次、稳定错误码 | 节点输入输出、完整 State |
| Generation | Provider、模型、usage、耗时、finish reason | Prompt、回答正文、reasoning |
| Tool Span | 工具名、`task_id`、状态、耗时、稳定错误码 | 工具参数、原始结果、凭证 |
| Verifier Span | verdict、faithfulness、引用错误数、风险数量 | 回答正文、证据正文 |

业务 `trace_id` 作为 metadata 保留；Langfuse/OTel Trace ID 由业务 `trace_id` 确定性派生。`session_key` 使用带应用命名空间的 SHA-256 摘要后作为 Session ID。MVP 不发送 `user_id` 和原始 `tenant_id`。

成本由 Langfuse 根据模型和 usage 计算。若当前 DeepSeek 模型不在内置价格表中，在 Langfuse Project 中配置 Model Definition；业务代码不硬编码价格。

## 七、敏感数据保护

保护分为两层：

1. 埋点时不设置 Prompt、回答正文、reasoning 和原始工具数据；
2. 使用官方 `mask_otel_spans` 在导出前删除所有已知输入、输出、Prompt、completion 和 reasoning 属性，作为纵深防御。

masking 使用确定性、无网络调用的字段删除规则。导出规则采用允许列表和明确的禁止字段集合；不得使用“扫描到疑似敏感内容再替换”作为唯一保护机制。

## 八、失败与生命周期

- 配置关闭时不初始化 Langfuse，也不创建 Callback。
- 显式启用但 Key 不完整时配置校验失败，防止假成功。
- Cloud 网络错误、限流、认证失败和导出失败不得改变业务响应。
- Langfuse 不加入 readiness 必要依赖。
- SDK 负责后台批处理和重试，请求线程不等待 Cloud 返回。
- 应用正常退出时调用官方 flush/shutdown，并设置最多 5 秒的清理预算。
- 日志只记录稳定错误码和异常类型，不记录密钥、Payload 或服务端响应正文。
- 现有业务 Trace 的采样、存储和保留策略不依赖 Langfuse。

## 九、代码边界

预计新增或修改的职责如下，具体路径在实施计划中锁定：

- `Settings`：Langfuse 开关和密钥校验；
- 本地启动器：加载 `.env.local`；
- Langfuse 初始化模块：构造官方 Client、Span Processor、Callback 和 masking；
- Graph 调用入口：注入官方 Callback，传播 Session 与业务 metadata；
- DeepSeek 客户端边界：用官方 Generation Observation 包裹直接 HTTP 调用并提交已有 usage；
- 应用生命周期：启动初始化与关闭 flush；
- 文档：本地配置、Cloud Project 设置和人工验证步骤。

初始化模块不承担 Trace 数据模型、网络重试、队列、成本公式或持久化职责。

## 十、测试与验收

自动测试只验证本项目自己的接线和安全边界，不重复测试 Langfuse SDK 内部实现：

- 配置关闭时不创建 Langfuse 运行时；
- 启用时必须同时提供 Public Key 和 Secret Key；
- `.env.local` 不被 Git 跟踪，启动器能够加载它；
- masking 删除输入、输出、Prompt、completion 和 reasoning 属性；
- Callback 被传入 Graph 调用；
- 直接 DeepSeek 调用将已有模型名、usage、耗时和 finish reason 交给官方 Generation API；
- Langfuse 运行时异常不改变业务调用结果；
- 应用关闭时调用一次官方 flush/shutdown；
- 现有业务 Trace、Graph 和会话测试继续通过。

真实 Cloud 验收使用一条开发请求，确认：

- Trace 能在 Japan Project 中出现；
- Graph 与 Generation 层级可读；
- 模型、Token、耗时和成本可见；
- Session ID 是脱敏值；
- 页面中没有 Prompt、回答正文、reasoning 和工具原始数据；
- 相同业务 `trace_id` 能在 Langfuse 与本地 Trace 间关联。

自动测试不访问 Langfuse Cloud，也不使用真实 Project Key。

## 十一、范围外事项

本轮不包含：

- Langfuse 自托管；
- 生产环境启用和生产采样策略；
- 在线 Evaluator、Dataset、Prompt Management；
- 自建 Langfuse 页面或复制其前端源码；
- 把 Langfuse 改造成业务审计或 Checkpoint 数据源；
- 上传原始 Prompt、回答、reasoning、工具参数和工具结果；
- 删除或合并现有业务 Trace 表。

## 十二、参考资料

- [LangChain 与 LangGraph 官方集成](https://langfuse.com/integrations/frameworks/langchain)
- [现有 OpenTelemetry 环境接入](https://langfuse.com/faq/all/existing-otel-setup)
- [Langfuse Masking](https://langfuse.com/docs/observability/features/masking)
- [Token 与成本追踪](https://langfuse.com/docs/observability/features/token-and-cost-tracking)
- [Environment 管理](https://langfuse.com/docs/observability/features/environments)
