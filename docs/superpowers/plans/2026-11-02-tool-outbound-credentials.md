# 工具外发凭证拦截实施计划

目标：按用户确认“尚无业务分类，先完成通用凭证拦截并保留业务规则接口”，阻止凭证通过公共工具参数与搜索上下文进入外部服务。

架构：沿用统一内容策略设计第 4 节，在独立安全模块中提供只判断不改写的 ToolOutboundPolicy。公共网关在观测、回执预留和派发前检查原始/实际参数、受信工具契约声明的上下文字段；两个搜索客户端在最终 payload 外发前再次检查，包括搜索续轮。认证 Header 仍由受信适配器注入，不作为模型业务参数处理。业务规则只接入受信字段拒绝规则，默认空，不推定手机号/财务数据敏感，不声称解决任意内部资料诱导外传。

技术：Python、Pydantic、现有纯内容凭证底线、pytest/httpx.MockTransport；不修改业务 Agent 节点，不修改外部 RAG 工程。

- [x] 新建 test_tool_outbound_policy.py：凭证文本/嵌套 JSON/Cookie/PEM、正常财务文本与手机号、业务字段路径/租户/权限规则、故障及未修改输入；先确认缺少策略导致失败。
- [x] 新建 security/tool_outbound.py：冻结决策、规则 schema、预算和凭证底线；字段规则命中即拒绝，无原文诊断，不接受工具自报授权。Settings 校验和环境示例配置业务规则，默认空。
- [x] 强化 ToolCallRequest 的凭证检查，避免已知凭证形成合法请求对象；网关仍独立复核以覆盖 model_copy/construct 和 input_model 处理后参数。规则错误/检查故障拒绝并标记 not_started。
- [x] 在受信 ToolSpec/Tool 注册契约中冻结 outbound_context_fields；WebSearchTool 声明 research_context，网关统一检查，不靠模型主动声明需要检查的字段。
- [x] 两个搜索客户端对最终 payload（含旧搜索续轮回传内容）做凭证底线检查，放在外部请求及相关观测输入之前；测试断言 HTTP 请求次数为 0，续轮场景只有已完成的首轮。
- [x] 验证授权、审批参数指纹、恢复、正常搜索、MCP/RAG 与原生消息回归；独立审查后更新中文验收材料、红队 Prompt 与 TODO。

验收不包含：业务分类自动推断、任意改写/编码的内部资料识别、全局模型输入与历史 checkpoint 清理、认证 Header 禁止使用或真实外部攻击。明确区分工具 not_started 和搜索首轮已发生后的续轮拒绝，不伪造副作用状态。

本批实施与本地验收完成：964 项关联测试通过，其中专项 32 项，独立复核通过。证据见 [验收记录](../../security/reports/2026-11-02-tool-outbound-implementation.md)。业务分类、完整外发用途控制和全局模型/历史内容入口仍属总体安全 TODO。
