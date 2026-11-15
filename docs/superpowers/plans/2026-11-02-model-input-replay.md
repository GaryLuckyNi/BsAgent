# 通用模型输入与旧历史重放保护实施计划

目标：延续用户确认的通用凭证保护，在普通模型请求和原生工具绑定入口为旧历史/ToolMessage/TaskResult 生成安全副本；不修改 checkpoint 控制数据或业务 Agent 节点。

架构：新增 security/model_input.py，复用共享凭证内核处理普通 prompt 和消息；旧 ToolMessage 经已有工具内容适配按当前策略重投影，角色与协议 ID 保持。llm/factory 在客户端选择/重试前处理 prompt，DeepSeekClient 在直调、同步/异步流式、SDK callback 前同样保护；bind_tools 返回受保护 Runnable，覆盖当前 sync/async invoke/stream 与继承 batch 路径。native_calls 在绑定调用前检查消息，兼容注入的其他客户端。认证 Header 仍由受信客户端注入。

预算：普通模型 prompt 使用独立的有限输入预算（单字符串最多 1,000,000 字符、总计 2,000,000 字符、50,000 节点、32 层），以容纳正常较长上下文；工具资料继续服从原工具策略预算。失败/超限在模型请求与相应 generation 观测前固定拒绝，不记录原始异常。

- [x] 写失败样本：factory 不同客户端能力分支及重试、DeepSeek 普通/同步异步流式/SDK callback、原生工具绑定、旧 ToolMessage 与嵌套 TaskResult、策略故障和正常对照。
- [x] 纯安全适配：生成 prompt/消息副本，角色不提升，工具调用及审批协议身份不悄悄改写；模板 schema 的字段名保留，凭证材料不进入工具说明。
- [x] 公共入口接入：factory、当前 DeepSeek 全部公开输入方法、native_calls；在 generation callback/HTTP 请求前执行，不把输出流过滤冒充输入防护。
- [x] 验证 Runnable 配置、绑定、重试/批量和调用 ID 保持；用内存 HTTP transport 验证实际请求/认证 Header/正常流式输出。
- [x] 使用真实 InMemorySaver 中断恢复，验证旧历史安全视图被模型实际接收，而 checkpoint 原始数据、审批参数和执行指纹不变。
- [x] 关联回归、独立复核和中文文档/TODO。

明确限制：本批不清理历史数据库，不自动推断业务敏感类别；无可靠工具来源的历史文本仅执行凭证底线，不声称完整重应用某工具的业务字段规则。全应用日志/上游链路观测、模型输出流新生成的敏感信息、任意第三方客户端旁路与真实模型多轮注入仍需专项验收。安全副本不能改变权限目录、任务上限或已经发生的工具副作用。

本批完成：1311 passed、6 skipped（5项真实PostgreSQL迁移、1项真实模型评测），新增专项35项；独立复核通过。JSON安全副本修复进入共享策略v3。证据与限制见 [验收记录](../../security/reports/2026-11-02-model-input-implementation.md)。
