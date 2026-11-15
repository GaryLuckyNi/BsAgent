# Langfuse 本地观测说明

Agent/RAG 的日常使用、评测与优化流程见[使用与优化闭环指南](langfuse-usage-and-optimization.md)。2026-09-26 的 EU 验收材料仅作为历史记录，见[归档报告](acceptance/archive/2026-09-26-langfuse-eu-smoke.md)。

## 权限恢复事件（2026-10-24）

恢复观测使用普通 span，不新增 Generation。固定事件为 result_classified、receipt_settled、availability_skip、recovery_batch_sealed、recovery_decision、recovery_invalidated、recovery_dispatch、recovery_result。只输出有界的 request/call/batch 引用、规则/阶段/结算原因、拒绝范围、预算动作、覆盖状态和路由原因码，不复制 query、answer、参数或原始错误。

`task_scheduler` 与 `dispatch_task_scheduler` 的安全摘要增加 recovery 批次引用、实际派发成员、恢复结果引用和覆盖状态。permission_recovery_confirmed 表示确认已持久化后进入独立恢复节点；settled_peer_allows_deferred_dispatch 表示继续从未远端派发的原参数本地等待，不是重试已拒绝 MCP。

recovery_dispatch 中 execute/reuse/blocked/exhausted 来自独立回执预留；ordinary_search 与 permission_recovery 区分普通搜索和批次替代，共享 Run 预算。receipt_settled 在落库之后导出，低风险原有豁免单独标明；不把真正 unknown 记为 settled。观测可能重复或丢失，不作为幂等执行证明，业务事实以回执/持久化决定为准。metadata_only 不强制导出这些正文，导出故障不影响业务。

受控真实 SDK＋内存导出器验收见[第三检查点](acceptance/2026-10-24-runtime-capability-recovery.md)。本次没有查询线上 Trace 或发起真实模型请求，不能据此声称线上延迟已经降低或真实源权限已验收。

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

日常本地开发可直接使用 `npm run dev`：启动器会自动执行上述初始化和 Compose 启动，再将生成的本地项目 key 注入 Backend 进程。`npm run dev:stop` 会停止 Langfuse 容器并保留数据卷。

当启动器确认 Docker 镜像仓库发生 EOF、DNS 或 TLS 超时时，默认会继续启动其他服务，并仅在本次 Backend 进程中设置 `BS_AGENT_LANGFUSE_ENABLED=false`。配置错误、鉴权错误和端口冲突仍会终止启动。如需任何 Langfuse 失败都终止开发环境，设置 `BS_AGENT_DEV_LANGFUSE_REQUIRED=true`。

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

### ChatOpenAI 与业务节点输入输出

`ChatOpenAI` 是 LangChain 调用路径的 Generation 名称，`deepseek.chat.completions` 是项目直连路径的名称，两者都可以调用同一 DeepSeek 模型。观测内容处理不会切换调用路径、增加模型请求或新增重复 Generation。

自动采集的内容在官方 `mask_otel_spans` 导出出口进行 JSON 解码、脱敏和长度限制；仅替换导出副本，不改写业务 State、模型请求和响应。受控内容规则如下：

| 观测记录 | Input | Output |
| --- | --- | --- |
| ChatOpenAI 等 SDK 自动 Generation | 脱敏后的消息、提示词及工具定义 | 脱敏后的模型正文、工具调用 |
| deepseek.web_search.turn 手工 Generation | 搜索系统指令、可信研究目标和可读续轮消息 | 回答、结束原因、搜索调用建议与本轮 provider usage；不记录续轮 encrypted_content |
| intent_router 节点 | 用户问题、消息上下文、图片摘要（不含 OCR 全文） | intent、errors |
| planner_agent 节点 | 用户问题、intent、消息上下文、已有 plan、task_results、迭代次数 | plan、planner_decision、停止原因、迭代次数、errors |
| task_scheduler 节点 | 计划任务、依赖、结果状态、就绪/阻塞任务、Planner 评审状态及反馈预算、待审批/用户确认标记、Skill 状态 | 本次局部更新：tool_search_count、planner_stop_reason、pending_user_input 是否存在；无更新时显示 `{}` |
| dispatch_task_scheduler span | 与调度节点一致的安全调度摘要 | next_node/next_nodes、reason、中文 explanation、派发任务 ID、Skill 步骤引用；普通任务派发时附带剩余搜索名额 |

未列出的节点仍不导出自动采集的输入输出，自动 Trace 级正文和任意 metadata 也不放行。JSON 字符串中的嵌套提示词及工具 arguments 同样经过敏感键清理，超出大小/深度限制或无法安全转换的字段会被丢弃；过长的安全内容以 `truncated` 包装展示。

查看方式：打开新生成的 Trace，展开 `intent_router` 或 `planner_agent`，节点的 Input/Output 显示业务材料及决策；其下的 `ChatOpenAI` Generation 显示实际提示词与模型响应。

### 调度与返回 Planner 的阅读方法

`task_scheduler` 更新状态，随后 `_dispatch_after_scheduler` 条件路由决定下一节点。展开这个条件路由内的 `dispatch_task_scheduler` span，即可查看实际命中的分支；分发摘要不会包含 `Send.arg` 的完整 State，不上传工具结果正文、用户确认问题/选项或 Skill 原文。并行派发多个不同节点时 `next_node` 为 `null`，以 `next_nodes` 和 `dispatched_tasks` 为准。

| reason | 含义 |
| --- | --- |
| ready_tasks / skill_task_ready | 按依赖派发普通任务 / 串行启动 Skill |
| new_results_require_review | 当前计划有未评审的新结果，返回 Planner；工具成功也可能命中 |
| planner_finished | Planner 已评审当前结果并决定结束，进入 Synthesizer |
| no_ready_tasks | 当前无就绪任务，进入 Synthesizer |
| planner_stopped | 已有停止原因；具体值见 planner_stop_reason |
| planner_feedback_budget_exhausted | 未评审结果需要反馈，但反馈预算耗尽 |
| user_input_required / permission_fallback_resume | 等待用户确认 / 用户确认后先恢复原权限回退任务 |
| approval_required | 先处理工具或记忆审批 |
| skill_workflow_dispatch / skill_workflow_failed | 继续 Skill 子步骤 / Skill 失败后转汇总 |

当前策略会优先让 Planner 评审当前计划的新结果，再派发后续就绪任务。因此看到 `tool_agent → task_scheduler → planner_agent` 不代表工具失败，也不一定代表重新拆分计划。结合分发 Input 的 `has_observations=true`、`reviewed=false` 和结果 `status`，再看 Planner 的实际 `planner_decision`，才能区分评审、继续、修改计划或结束。

`planner_decision.synthesis_task_id` 表示 Planner 显式将唯一剩余的终端分析交给最终回答节点，`planner_finished` 之后不再重复执行 Analysis 和最终复核。没有该字段则沿用原有 continue/finish 行为；它不是调度层静默删任务。仅改摘要的无效 revise 会在本地规范化，观测中的 iteration、任务 ID 保持不变。

调度输入中的 `ready_task_ids` 只是依赖已满足的任务，不承诺立即派发：用户确认、审批、Skill 工作流和 Planner 评审拥有更高优先级。`tool_search_count` 是工具目录发现次数，不是联网搜索消耗；普通派发 Output 的 `remaining_web_search_slots` 才是现有策略计算出的剩余联网搜索名额。

调度观测本身不改变调度顺序、权限或预算，不新增模型调用；终端分析的显式交接规则见 `docs/graph-runtime.md`。需要后端/worker 加载新代码后发起新请求；旧 Trace 无法追补已删除的输入输出。

### 搜索内部用量

搜索服务每次实际模型 HTTP 请求对应一个 `deepseek.web_search.turn` Generation。一轮返回即可只记一次，发生续轮则分别记录两次，每个 Generation 仅写本轮 usage；搜索响应的整体 usage 为各轮累加，不能再把它作为额外 Generation 重复计费。该请求为非流式，不伪造首 Token 时间。搜索调用包含服务端原生工具，Generation 耗时不等于纯模型推理耗时。

查看模型实际输入应以 Generation 为准：节点 State 中的 `ToolMessage.content`、`artifact` 和 `task_results` 属于执行/审计副本，不表示这些副本全部发送给了模型。当前输入投影只压缩实际消费者上下文，不删除审计记录。

`BS_AGENT_LANGFUSE_CONTENT_MODE=local_content` 允许上述脱敏内容；`metadata_only` 仍不采集正文。默认不展示 reasoning，只有 `local_debug` 且 `BS_AGENT_LANGFUSE_CAPTURE_REASONING=true` 才允许。该修复仅影响加载新代码后产生的记录，无法补回旧 Trace 已删除的正文；修改后需重新加载后端/worker，不需要数据库迁移。

本项目当前使用 Langfuse 4.16.0 SDK。SDK 及本地平台不可用时，业务 Agent 不应因观测写入失败而阻断；退出阶段执行 flush/shutdown。成本由本地项目模型定义估算，需核对模型名称和价格，不能当作供应商账单。

## 4. Scores、Evaluator 与人工复核

本地项目维护七种稳定 Score：`task_success`、`answer_quality`、`groundedness`、`selection_correctness`、`citation_coverage`、`action_policy_compliance`、`review_outcome`。Numeric 分数限制在 0–1；成功/合规项使用 Boolean；`review_outcome` 为 accept、needs_revision 或 reject 分类值。

规则 evaluator 计算可确定的任务完成、引用覆盖、工具选择与操作策略结果；DeepSeek evaluator 对回答质量和 groundedness 做结构化判断。Judge 不可用或输出无效时标记评估失败，不伪造低分，也不改变业务运行结果。Evaluator 的判断应可追溯 rubric、evaluator、dataset 与 run 版本。

Judge 的 `research_evidence` 包含成功外部工具材料、共享来源和 RAG 证据，整体脱敏后限制为 12,000 字符并显式标记截断；不把 Analysis/最终答复回声或失败结果当成独立佐证。只有 URL/标题、调用 success 或搜索自述 coverage 都不能证明正文主张。该补充不改变 Judge 启用开关、采样策略或后台执行方式，也不增加调用次数。

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
