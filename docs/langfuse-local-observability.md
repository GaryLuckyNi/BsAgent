# Langfuse 本地观测说明

Agent/RAG 的日常使用、评测与优化流程见[使用与优化闭环指南](langfuse-usage-and-optimization.md)。2026-09-26 的 EU 验收材料仅作为历史记录，见[归档报告](acceptance/archive/2026-09-26-langfuse-eu-smoke.md)。

## 1. 定位与运行版本

本项目目标平台为本机 Langfuse 4.16.0，Web UI/API endpoint 为 `http://localhost:3000`，Python SDK 锁定为 4.16.0。真实验收时必须从 `/api/public/health` 回读运行版本；代码或 compose 中的版本号只能说明期望版本，不能代替运行证据。Langfuse 用于查看 Agent/RAG 执行树、Generation、Token、耗时和成本；它不替代 PostgreSQL 中的业务 AgentRun、审批和审计数据。Alerts 与 Playground 本轮不在实施范围内。

| 数据 | 用途 |
| --- | --- |
| PostgreSQL 业务表与 Agent Trace | AgentRun 状态、审批恢复、审计和受控排障 |
| 本地 Langfuse | Agent/RAG Trace、Generation、Scores、人工标注、Prompt、Dataset/Experiment |
| 现有 OpenTelemetry 后端 | HTTP、数据库和进程基础设施遥测 |

## 2. 本地配置与启动

在仓库根目录准备 `.env.local`，保留其中其他开发配置，并设置：

```dotenv
BS_AGENT_LANGFUSE_ENABLED=true
BS_AGENT_LANGFUSE_BASE_URL=http://localhost:3000
BS_AGENT_LANGFUSE_PUBLIC_KEY=pk-lf-本地项目PublicKey
BS_AGENT_LANGFUSE_SECRET_KEY=sk-lf-本地项目SecretKey
BS_AGENT_APP_ENV=local
BS_AGENT_LANGFUSE_SAMPLE_RATE=1.0
```

密钥从本地 4.16.0 项目取得，只放在被 Git 忽略的 `.env.local` 或 Secret Manager，不得写入文档、命令输出或提交记录。Shell 已设置的变量优先于 `.env.local`。本地 Langfuse 的 compose、初始化和示例流程见 [Langfuse 本地运行计划](superpowers/plans/2026-10-05-langfuse-local-observability-examples.md)。

启动本地 Langfuse：

```bash
python3 langfuse/init.py
docker compose --project-directory langfuse -f langfuse/compose.yaml up -d --wait
```

打开 `http://localhost:3000`。启动 Agent 后端并发起合成或经授权的本地请求：

```bash
npm run dev:backend
```

本地 smoke 分为两类。默认命令是无模型费用的**合成平台探针**，用于验证 API、层级、Scores、Evaluator、人工队列、Prompt、Dataset 和 Experiment；其中写入的 usage/成本是明确标记的合成值，不能作为 DeepSeek 验收证据：

```bash
.venv/bin/python langfuse/smoke.py
.venv/bin/python langfuse/smoke.py --debug-reasoning
.venv/bin/python langfuse/smoke.py --verify-human-annotation
```

真实验收必须显式执行以下命令。它安全读取 `.env.local` 和本地平台配置，运行一条真实 Agent Graph、受控本地 RAG/只读工具 observation 和真实 DeepSeek generation，因此会产生少量模型费用：

```bash
.venv/bin/python langfuse/smoke.py --real-agent \
  --output docs/acceptance/2026-10-05-langfuse-local-real-smoke.json
```

`passed=true` 才表示本次真实验收通过。脚本回读 `bs-agent.run` 层级、`business_trace_id`、DeepSeek provider usage、成本和内容策略；服务、凭证、模型或回读不可用时返回 `blocked=true` 与稳定 `blocker`，不会用合成记录顶替真实结果，也不会输出异常正文、密钥或原始 canary。若本地模型价格未配置而成本字段为空，`deepseek_cost_present=false`，本次真实验收失败，应先配置价格再重跑。

`--debug-reasoning` 只运行独立合成 reasoning canary；人工标注验证要求先在 UI 完成两个队列中的指定项目。示例操作使用 `.venv/bin/python scripts/langfuse_examples.py all`。Dataset 统一命名为 `bsagent-local-golden-v1`。

## 3. Trace 与内容边界

一次 AgentRun 应能在 Langfuse 中关联 `business_trace_id`，并呈现 Agent 根节点、Skill/Graph 节点、Tool、RAG 检索阶段和 Generation。当前 `local_content` 契约如下：

| 内容 | `local_content` | `local_debug` 且显式开启 reasoning |
| --- | --- | --- |
| prompt、回答 | 脱敏、截断后进入本地 Trace | 同左 |
| RAG query/chunk、工具参数/结果 | 仅受控内容经递归脱敏、截断后进入 | 同左 |
| reasoning/reasoning_content | 删除，只保留字符数等非内容指标 | 经显式开关后可进入本地 Trace |
| API key、Authorization、Cookie、密码、Token、文件/二进制正文 | 删除或替换 | 仍删除或替换 |

`local_content` 并非“只上传指标”。它允许脱敏后的业务内容进入本机 Langfuse，以支持内容观测；真实用户数据仍须先满足组织的数据处理要求。`local_debug` 的 reasoning 开关只用于短时、受控排障，完成后应关闭并按本地保留策略清理。

Generation 展示模型、模型参数、耗时、TTFT、Token 和可用的成本估算。直连模型请求应显式记录 provider usage；非流式请求不伪造 TTFT。Graph 重试会形成多次调用，应区分失败尝试和最终成功结果。业务成功以 AgentRun 最终状态和业务结果为准。

本项目当前使用 Langfuse 4.16.0 SDK。SDK 及本地平台不可用时，业务 Agent 不应因观测写入失败而阻断；退出阶段执行 flush/shutdown。成本由本地项目模型定义估算，需核对模型名称和价格，不能当作供应商账单。

## 4. Scores、Evaluator 与人工复核

本地项目维护七种稳定 Score：`task_success`、`answer_quality`、`groundedness`、`selection_correctness`、`citation_coverage`、`action_policy_compliance`、`review_outcome`。Numeric 分数限制在 0–1；成功/合规项使用 Boolean；`review_outcome` 为 accept、needs_revision 或 reject 分类值。

规则 evaluator 计算可确定的任务完成、引用覆盖、工具选择与操作策略结果；DeepSeek evaluator 对回答质量和 groundedness 做结构化判断。Judge 不可用或输出无效时标记评估失败，不伪造低分，也不改变业务运行结果。Evaluator 的判断应可追溯 rubric、evaluator、dataset 与 run 版本。

设置两个 Human Annotation queue：`answer-quality-review` 用于 Trace 级回答质量、groundedness、操作合规和复核结论；`skill-tool-selection-review` 用于具体 Skill/Tool 选择 observation。自动策略可按低分、规则与 judge 不一致、失败或抽样结果将样本加入待复核队列。人工完成后记录 Score 与简短备注；备注不得包含秘密。

## 5. Prompt、Dataset、Experiment 与优化闭环

Prompt Management 保存有版本的模板，通过标签选取当前版本，并把 Prompt 关联到 Generation。Dataset 使用稳定 ID 保存输入、期望结果、允许工具、引用要求和风险级别；先使用合成样本，避免混入凭证或未经审查的用户数据。Experiment 对固定 Dataset 运行指定 prompt/graph/skill 版本，以规则 evaluator 评分，并可显式启用 DeepSeek judge。

建议按以下顺序形成闭环：

1. 从 Trace、业务反馈和人工队列发现具体失败类型。
2. 将可安全复现的样本整理进有版本的 Dataset，并记录当前基线。
3. 修改 Prompt、检索配置、Skill 或执行策略，记录版本差异。
4. 对相同样本运行 Experiment，对照七类 Score、耗时和 Token。
5. 检查退化项和人工复核结果，再决定是否推广；失败样本回流 Dataset。

本地示例命令：

```bash
.venv/bin/python scripts/langfuse_examples.py prompt
.venv/bin/python scripts/langfuse_examples.py dataset
.venv/bin/python scripts/langfuse_examples.py experiment
.venv/bin/python scripts/langfuse_examples.py experiment --with-deepseek
```

上述 Dataset 名为 `bsagent-local-golden-v1`。每轮优化以同一 Dataset、同一价格口径和明确版本运行基线与候选 Experiment；失败 Trace 经人工复核后转为脱敏 case，修复后重新运行并把结果关联回原问题，才形成“发现 → 复现 → 修改 → 对照 → 门禁 → 回流”的闭环。

## 6. 排障与停用

| 现象 | 检查顺序 |
| --- | --- |
| UI 无法打开 | 检查本地 compose 服务状态及 `http://localhost:3000` 健康状态 |
| 看不到 Trace | 检查开关、endpoint、项目 key、environment、采样率和后台 flush |
| Generation 有 Token 但无成本 | 核对模型名与本地 4.16.0 项目的模型价格定义，再生成新请求确认 |
| 回答慢 | 从根节点定位慢节点，再比较 Generation TTFT、输出量和重试 |
| 发现凭证 | 停止相关数据写入，轮换暴露凭证并按本地事件流程清理受影响数据 |

停用可设置 `BS_AGENT_LANGFUSE_ENABLED=false` 并重启后端。Langfuse 不属于 Agent readiness 的强依赖。更多字段、RAG 限制与优化步骤见[使用与优化闭环指南](langfuse-usage-and-optimization.md)。
