# Generation 延迟诊断：真实 Trace 与小规模模型实测

## 范围与方法

- 日期：2026-10-22，时间按北京时间说明。
- 平台：本机 Langfuse，健康接口回读版本为 `4.16.0`。
- 使用 `/api/public/v2/observations` 读取最新根 observation 及关联 trace。当前部署为 events_only，旧版 traces/observations API 返回 404，不能据此判断平台不可用。
- Trace：`2ee0f9190754c0f9c24118d6669f36ed`，项目 `bsagent-content-local`。
- 用户输入：“你觉得现在a股市场什么时候才能止跌”。开始时间 21:07:46.752，主流程结束时间 21:08:25.413。
- 按 systematic-debugging 流程先收集证据，再形成优化优先级。本轮未修改业务代码、模型配置或工具权限。
- 额外发起 7 次真实 DeepSeek 请求；不执行模型返回的任何工具，不重跑整条业务流程。测试产生少量模型费用。

## 主流程时间分解

| 阶段 | 墙钟耗时（秒） | 说明 |
| --- | ---: | --- |
| intent_router | 1.061 | 1 次 Generation，模型调用 1.050 秒，输出 54 token |
| 初始 planner | 4.464 | 1 次 Generation，模型调用 4.409 秒，输出 706 token |
| 第一批工具任务 | 8.346 | 3 个任务并行；每个任务都进行了两次模型调用；2 次搜索分别为 5.330、5.037 秒 |
| planner 复核、修订 | 7.391 | 2 次 Generation，分别为 4.401、2.853 秒；第二次为校验失败后的重试 |
| 第二批工具任务 | 6.336 | 2 个任务并行；每个任务都进行了两次模型调用；搜索 4.302 秒 |
| planner 最终复核 | 1.726 | 1 次 Generation，模型调用 1.644 秒，输出 148 token |
| synthesizer | 8.724 | 模型调用 8.692 秒；有效首内容 0.968 秒；输出 1325 token |
| 其余节点及阶段间隙 | 0.613 | 调度、校验、状态处理等剩余时间 |
| 合计 | 38.661 | 不能将并行分支耗时或父子 span 重复相加 |

主流程有 16 个可见 Generation，另有 1 个主流程结束后的评估 Generation（1.979 秒），不计入上述 38.661 秒。web_search 是 TOOL observation，其内部供应商调用未作为独立 Generation 展开，因此可见 Generation 数不等于所有底层模型请求数。

## 已确认的额外耗时来源

### 1. 工具调用数量不符合执行器契约

5 个工具任务首次均输出多个 tool call，但 `backend/app/tools/native_calls.py` 只接受最多一个调用；`backend/app/graph/nodes/tool_agent.py` 捕获后添加纠正提示并重新调用模型。trace 中对应的校验错误是“最多调用一个工具”。

这产生 5 次额外 Generation，耗时分别为 0.907、0.887、1.209、0.633、0.989 秒，合计 4.625 秒调用耗时。因为存在并行，该总和不是可直接扣除的端到端时间。

初始提示虽写了“选择一个候选”，并未可靠约束模型输出。不能仅以 `tool_choice=required` 修复：该参数还会改变允许不调用工具的业务行为。

### 2. Planner 修订结果包含未知依赖

首次修订结果的分析任务通过 `depends_on` 引用了已不在新 plan 中的旧任务 `policy_and_macro_catalyst_check`，同时也将它放在 `evidence_task_ids` 中。校验报错：

> 任务 synthesis_stop_falling_conditions 引用了未知依赖。

系统因此额外请求一次模型，耗时 2.853 秒。第二次输出将该旧任务保留为证据引用，移出新计划的执行依赖。

### 3. 证据时点错误和工具拒绝引发新一轮取证

用户问当前市场，但首次估值搜索 query 指向 2025 年，返回了旧年份资料。planner 正确识别时点不符，要求重新取证。

两个行情数据工具分别被拒绝，错误码均为 `tool_side_effects_unresolved`。本轮确认拒绝结果及它对重新规划的影响，未确认底层未决副作用状态是否正确，也没有绕过或清除该保护机制。

因此，直接删除 planner review 会掩盖本次真实存在的证据问题。

## 单次模型请求实测

使用当前本地配置，配置模型名为 `deepseek-v4-flash`，供应商响应返回模型名 `deepseek-flash`。请求根据 trace 中的脱敏消息重建；嵌套内容重新序列化，不声称字节完全相同。

### Router：同一输入连续 3 次

每次输入 571 token、输出 54 token；每次缓存命中 384 token。三次共享一个 HTTP 客户端。

| 请求 | 总耗时（秒） | 首流事件（秒） | 首段非空内容（秒） |
| --- | ---: | ---: | ---: |
| 第 1 次 | 1.965 | 1.274 | 1.746 |
| 第 2 次 | 0.716 | 0.166 | 0.487 |
| 第 3 次 | 0.771 | 0.163 | 0.547 |

结果表明该输入并非每次必然超过 1 秒。首次请求更慢，但本轮未单独测量 DNS、TCP、TLS、排队等阶段，不能将差值全部归因于连接建立。两次后续结果也不足以代表长期 P50/P95。

### Tool agent：默认参数与单工具参数对照

使用首次政策任务的消息和工具定义，保留 `tool_choice=auto`、关闭 thinking；所有测试统一设置 `max_tokens=1024`。两组交替运行，调用同一供应商地址；不实际执行工具。

| 请求 | 总耗时（秒） | 返回 tool call 数 |
| --- | ---: | ---: |
| 默认，第 1 次 | 0.934 | 2 |
| `parallel_tool_calls=false`，第 1 次 | 1.662 | 4 |
| 默认，第 2 次 | 1.372 | 2 |
| `parallel_tool_calls=false`，第 2 次 | 0.987 | 2 |

当前 API 接受请求，但参数未在这两次样本中实现单工具约束。因此不能将该参数作为已验证有效的修复方案，也不能根据这两个样本断言它会导致请求更慢。后续应验证更明确的输出契约、提示词及结构化输出方案，并保留本地校验。

## 首 token 口径纠正

此前仅凭 `.invoke()` 判断 router/planner 为非流式调用是不准确的。实际图以 `stream_mode=["updates", "messages", "custom"]` 运行；LangChain 可以根据流式 callback 隐式选择底层 streaming 路径。

当前 Langfuse callback 在第一次 `on_llm_new_token` 回调时就记录 `completionStartTime`，未检查内容是否为空。因此 trace 中 ChatOpenAI 的约 0.14–0.33 秒数值可能是首个空流事件，而非首段有效正文或有效工具参数。本轮实测也观察到首事件显著早于首内容。

synthesizer 的直连实现等待非空正文或 reasoning 才记录首 token；本条 trace 的 thinking 已关闭，`first_token_kind=content`、`reasoning_chars=0`。其 8.692 秒中，首段正文前约 0.968 秒，其后约 7.724 秒。后者包含持续输出、传输与本地处理，不能全部精确归因于服务端解码。

## 优化优先级

1. 优先减少已证实的格式与契约重试：工具数量约束、planner 新旧任务依赖和证据引用。任何候选改动都需以真实模型样本验证，不能只依赖参数名称。
2. 保持当前日期与目标时点贯穿工具输入；排查行情工具被拒绝的状态及工具可用性披露，避免规划不可执行路径。
3. 根据产品需求缩短实际输出：初始计划输出 706 token、最终回答输出 1325 token。降低 `max_tokens` 上限本身并不保证更快，应首先约束实际内容冗余且避免截断有效 JSON。
4. 统一首流事件、首正文、首工具参数、完整响应的计时，再对各角色做模型对照和延迟分位数评估。

本次有证据支持“生成量与反复调用共同放大耗时”，没有证据支持“所有慢请求主要由连接建立造成”或“换模型就能解决”。这是单条业务 trace 加 7 次小规模探针的结论，不是生产性能基准或已实施修复。

## 修复实施与验收补充

用户批准实施后，在 `dev` 完成以下修改：

- native tool calling 首次请求即增加 system 级别的单次工具调用契约；普通任务保持 `auto` 可不调用，固定 Skill 保持 `required`。多调用及未知工具仍在本地拒绝，不擅自执行模型返回的第一个调用。
- DeepSeek 工具绑定显式设置 `temperature=0` 和关闭 thinking。测试发现 `.bind(...).bind_tools(...)` 未保留新增的温度绑定，因此改为直接向 `.bind_tools(...)` 传递参数，并对最终 RunnableBinding 参数断言。
- 普通工具任务、固定 Skill 和 planner 复核均得到北京时间与时区。相对时间使用锚定日期；明确指定的历史期间保持不变。
- planner 解析修订计划前，将“已成功且不在新活动计划中的历史依赖”转换为证据引用。转换先验证单个任务，不掩盖重复字段；未知、失败、自依赖、循环及活动任务依赖仍保持原有严格校验。

### 修改后的实际调用代码重放

重建原始三个任务，每类请求两次，直接运行修改后的 `create_tool_call_message`、`_tool_call_messages` 和 DeepSeek 绑定代码，不执行任何返回工具，也不使用自动修复重试。

| 原任务 | 第 1 次（秒） | 第 2 次（秒） | 数量与日期结果 |
| --- | ---: | ---: | --- |
| 政策与宏观 | 1.117 | 1.052 | 各 1 次 web_search，query 指向 2026 年 10 月 |
| 指数及资金面 | 0.825 | 0.684 | 各 1 次资金流查询，日期为 20260901–20261022 |
| 估值与情绪 | 1.076 | 1.057 | 各 1 次 web_search，query 指向 2026 年 10 月 |

这 6 个样本均首次通过单调用校验；这是小样本验收，不表示未来模型一定遵守提示词，有限修复和本地校验继续保留。参数有效性和证据充分性仍由原有校验、工具结果和 planner 复核负责。

### 完整隔离流程

- 验收问题与原始问题相同。
- 独立新会话、内存 store、相同意图 taxonomy；仅开放低风险 `web_search`，使用真实工具检索索引，未执行 MCP。
- Langfuse trace：`88c0134cfd58b2138ad268c75048ef51`，北京时间 22:35:29 开始。
- Langfuse 根 observation 耗时 **24.221 秒**；测试进程测得 **24.261 秒**（含图准备等开销）。
- 共 **6 次 Generation**：router、初始 planner、2 次工具选择、planner 复核、synthesizer；没有工具数量或 planner 契约重试。
- 两次真实 web_search 分别耗时 **4.066、6.125 秒**；两条任务结果均成功，最终回答存在，业务 errors 为空。
- synthesizer 耗时 **11.116 秒**，输出 **1875 token**，首段有效正文 **0.682 秒**。本轮没有缩短最终回答，故最后输出阶段仍明显耗时。
- 与原 trace 的工具目录、会话状态、缓存与输出长度不同，不能将 38.661 秒到 24.221 秒的差值全部归因于修复，也不能作为稳定提速比例。
- 先前诊断夹具曾缺少显式工具检索索引，产生了一条没有实际搜索的测试 trace `161cf650af0f75f73018ab961ead578e`。该条不作为完整工具流程验收或性能证据；补齐索引后以上述 trace 为准。

### MCP 拒绝的已确认原因与保留边界

通过只读事务查询原会话 PostgreSQL 回执，发现历史工具 `mcp.tushare.fina_mainbz` 在北京时间 17:22:10 调度、17:22:11 收到无效响应，风险为 `medium`，状态为 `unknown`，错误码 `mcp_tool_response_invalid`。

执行控制会阻止同会话后续 medium/high 工具，因而原 trace 的资金流和指数查询被拒绝。本轮没有清除这条回执、自动认定此前调用未生效或批量降低 MCP 风险；需要核实该次历史执行结果，或单独完成可信的只读工具分类设计，才能解除原会话对应阻塞。隔离搜索验收不表示原会话的 MCP 阻塞已经消失。

### 本地回归

TDD 首轮新增用例得到 6 项预期失败，覆盖缺失的单调用契约、日期上下文、温度绑定及历史依赖转换。补齐实现后，对以下范围执行最终回归，共 **329 passed**：

```bash
.venv/bin/python -m pytest \
  backend/app/tests/unit/test_native_tool_messages.py \
  backend/app/tests/unit/test_deepseek_client.py \
  backend/app/tests/unit/test_planner_feedback.py \
  backend/app/tests/unit/test_dynamic_planner.py \
  backend/app/tests/unit/test_tool_agent.py \
  backend/app/tests/unit/test_skill_workflow.py \
  backend/app/tests/unit/test_durable_turns.py \
  backend/app/tests/unit/test_tool_call_budget.py \
  backend/app/tests/integration/test_dynamic_task_graph.py \
  backend/app/tests/integration/test_skill_workflow_graph.py \
  backend/app/tests/integration/test_session_api.py -q
```

修改的 7 个 Python 文件通过 `ruff check`，差异通过 `git diff --check`。回归覆盖 auto 不调用、required 固定调用、多调用有限修复、未知工具拒绝、历史时间保留、活动依赖不被转换、成功历史证据传递，以及失败/未知/重复/自依赖/循环引用仍拒绝。

## 用户补充确认后的权限问题修复

用户明确确认 `fina_mainbz` 的历史 unknown 来自无权限，并要求移除按接口名维护的只读白名单；交互仍为 interrupt 确认后搜索。已实现通用权限拒绝分类和独立文本拒绝兼容，不再依赖接口名单。上述“原会话 MCP 阻塞尚未解除”是前一阶段状态，现已定向结算用户确认的精确回执并保留原失败与审计；原会话 blockers=0，medium/high 副作用检查通过。

实现及候选约束见[通用权限拒绝规格](../superpowers/specs/2026-10-22-mcp-permission-generic-denial-design.md)，数据库核实见[回执记录](../mcp-permission-receipt-reconciliation-2026-10-22.md)。超时、执行不明、部分结果伴随权限错误的保护仍保留，不批量清除 unknown。
