# 投研执行效率修复验收

## 范围与原始依据

依据真实 Trace `e30146f25b95e96c4651e26a850c8501`，修复无效修订、终端分析重复、模型输入重复和搜索成本不可见。不调查用户明确认定为偶发的 61 秒等待，不改 Router 模型、接口、超时或重试策略。

分支：`codex/research-execution-efficiency`。保留开始前已有未提交修改，不创建 worktree，不提交或推送。

## 已完成

1. 同任务列表的 revise 本地转为 continue/finish，不消耗修订次数、不重命名、不发额外纠错请求。真正改变任务、依赖、验收条件或顺序仍按修订校验。
2. Planner 仅输出结构化 `synthesis_task_id`，把唯一就绪、依赖成功且无消费者的终端分析交给 Synthesizer；工具、Skill、中间分析和取证后的证据评审均保留。历史引用、用户材料与附件不会因交接丢失。
3. 模型消费者收到确定性投影：当前问题不再随完整会话重复出现；结果 JSON 对象化、来源共享；合成只接收一份缺口、不接收重复 query/调度 action/空字段。未知业务字段不删除，原始执行记录与 artifact 不改写。
4. 搜索通过服务端上下文绑定任务目标、验收条件和信息时点，续轮保留；coverage 与调用成功分离。未取得有效声明时为 unknown，长结果摘要也保留覆盖状态及明确截断标记。该自述不替代 Planner 评审。
5. 搜索每个真实模型 HTTP 请求记录一个 `deepseek.web_search.turn`，usage 分轮记录并累计；不写 API Key 或续轮加密正文。Judge 补充有界、脱敏的外部证据，不改采样或调用次数；附加证据无法安全解析时仅省略证据，不阻断整轮观测收尾。
6. 最终交接路径保留推理，输出上限为 6144 Token，普通问答正文以800至1200汉字为目标；普通报告合成仍为16384。Planner 提示可见该预算，长篇报告不使用交接路径。

## 自动化验证

- 全量后端：1620 passed、50 skipped、2 warnings。两条警告为既有 pytest 模块重写和测试 JWT 短密钥；环境相关跳过未伪装成通过。
- 最终相关回归：165 passed，覆盖真实业务 Graph、权限 interrupt/恢复、搜索上下文、输入投影及真实 Langfuse SDK 内存导出。
- 相同双取证 DAG 的确定性回放：8次主链路调用降为6次，仍有1次取证评审，不执行被显式交接的 Analysis 或第二次最终评审。此计数不包含搜索服务内部请求，不是实际供应商时延或质量基准。
- 本轮相关生产/测试文件 Ruff、`git diff --check` 通过。全库 Ruff 仍有范围外告警（例如 FastAPI `Depends` 的 B008），未扩展清理范围。
- 独立评审发现3个 Important：历史引用丢失、附加证据解析影响收尾、长摘要丢 coverage。均先复现失败再修复；复核32项通过，未发现残余 Important。

## 真实模型验收

### 隔离的完整链路

[完整链路 Trace](http://localhost:3000/project/bsagent-content-local/traces/f4c081e0b385900014f2212214bae837)

使用当前配置的真实模型，仅披露 web_search，空 Skill 目录，不执行 MCP 或业务数据库写入，关闭此次隔离测试的 Judge。问题为“分析一下今天a股暴跌是否探底”。最初一次隔离环境初始化缺少 Skill 目录，未执行搜索，不算成功样本；显式注入空目录后重新验收。

本次生成3个取证任务和1个最终分析任务，Planner 迭代1次、取证评审1次，实际返回 `finish + synthesis_task_id=bottoming_assessment`。无业务错误、无独立 Analysis 节点、无最终第二次 Planner 评审。

平台回读共10个 Generation：7个主链路调用（Router、初始Planner、3次工具选择、证据评审、最终合成），另有3个之前不可见的搜索内部请求。本次各搜索均一轮完成。搜索逐轮 usage 和父子关系已可见，估值覆盖未取得有效声明时确实以 unknown 进入合成输入，没有伪装为 complete。

该样本与旧链路任务数、目录和时点不同，不能据此计算节省比例。首次交接合成仍用原报告级16384上限，生成偏长，因此继续做固定证据预算验收。另有一条 Planner Generation 输入被安全导出策略省略，未用未脱敏正文回退；可查其业务节点决策及最终合成的实际输入。此处不放宽隐私边界。

### 固定证据的交接预算

- 4096上限测试：[Trace](http://localhost:3000/project/bsagent-content-local/traces/6e09a3c48cf46a3e534f6beae112b372)。平台实际返回 `finish_reason=length`，因此不接受其耗时为性能改善证据，也不保留该配置。
- 最终6144上限与普通答复篇幅指令：[Trace](http://localhost:3000/project/bsagent-content-local/traces/9211d83f152143e83912168bfa855808)。复用同批已取得的材料，不重新搜索、不调用 Router/MCP，平台回读 `finish_reason=stop`，正文2138字符（含免责声明）。

以上只验证链路、调用数量、上下文与正常结束，不构成统计性能基准或投研事实正确性/答案质量评测。保留模型推理，不以降低模型档位或关闭思考换取测试数字。Thinking 模式的输出预算含推理与正文，具体协议以 [DeepSeek 官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/) 为准。

## 生效方式与边界

后端/worker 重新加载后新请求生效，不需要数据库迁移。web_search 输出契约增加可选覆盖字段，目录摘要变化；旧 Checkpoint 被一致性保护拒绝恢复时应重新发起，不绕过目录/权限校验。旧 Trace 不追补。

MCP 权限失败仍先 interrupt，用户确认后才使用本轮实际候选中的 web_search；没有接口白名单、自动降级或工具重放。仍需通过后续业务样本评估工具候选相关性和答案质量，本轮未擅自缩减候选目录或改 Judge 采样。
