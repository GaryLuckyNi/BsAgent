# 通用模型输入与旧历史重放保护验收记录

日期：2026-11-02。范围：独立安全模块及普通模型/原生工具绑定公共适配；业务 Agent 节点、外部 RAG 工程未修改。未提交、部署、迁移或批量改写历史数据。

## 已完成

新增 `security/model_input.py` 与 `llm/input_guard.py`，复用同一凭证底线生成模型侧副本，接入以下当前路径：

| 路径 | 接入位置及行为 |
| --- | --- |
| 普通 completion、客户端能力选择与重试 | factory 每次派发前保护 system/user prompt，覆盖 async complete、callback、同步/异步原生流式能力分支及不带 thinking 的重试 |
| 当前 DeepSeek 公开输入方法 | 普通 complete、callback、stream_complete/events、同步/异步 streamed 在 HTTP/SDK 调用及对应 generation 观测前保护 prompt |
| 原生工具绑定 | DeepSeek bind_tools 返回受保护 Runnable，覆盖 invoke/ainvoke/stream/astream、继承 batch/abatch 及配置、bind、retry、fallback 组合；events/log 流入口同样先保护消息 |
| 注入的工具调用客户端 | native_calls 在模型消息调用前保护副本，工具 schema 在绑定前检查；不要求业务节点自行清理 |
| 旧 ToolMessage | 按当前工具内容策略重投影正文与 artifact，清理 kwargs/metadata；保留 ToolMessage 角色及实际执行信息，不把工具资料提升为系统指令 |
| 旧 TaskResult、历史与降级资料 | 它们经普通 prompt 或消息送入上述入口时执行凭证底线；原始 checkpoint、调用参数、审批及副作用记录保持原值 |

有可靠宿主 tool name 的 ToolMessage 可按当前工具规则投影；无可靠来源的历史自由文本只执行凭证底线，不推定已完整重应用某个工具的业务字段规则。业务分类仍未定义，普通公司名、手机号及金额不会因此自动禁止。

## 协议、模板及预算

消息角色保持原类型，已知敏感材料清理发生在模型侧副本；调用 ID/name 等协议身份若需要改变则拒绝，不能静默改成星号后继续。AIMessage 的有效/无效工具调用整体保护，包括无效调用的 error；旧 function_call 及 tool_calls 身份一并验证。

schema 的 properties、required 等字段名属于契约，保持不变；description 及 default/const/enum/examples 等模板数据若包含凭证则拒绝绑定，不能用“模板”绕过数据检查，也不改写已冻结的执行 schema。

普通模型输入预算为单字符串 1,000,000 字符、整组消息/普通 prompt 总计 2,000,000 字符、50,000 节点、32 层；协议字段与 artifact/调用错误也计入消息总预算。工具资料继续服从原工具内容预算。工具模板另有有限遍历预算。检查故障/超限在实际模型调用前固定拒绝，不携带原始异常链；配置 metadata 等宿主观测配置不因本层获得全局安全声明。

## 共享内核修复

多层输入保护暴露了旧扁平文本清理在 JSON 字符串内消费结束引号的问题。共享策略已改为先识别 JSON 结构，在解码后的字符串叶子与非 JSON 片段上清理，再保持有效 JSON 序列化；重复处理保持幂等。跨片段的凭证赋值/认证头无法安全界定时扣留整个字符串，不能只替换一部分留下秘密；前缀检测同样计入工作预算。

共享内容策略语义版本升级为 `tool-content-v3`。此前 v1/v2 验收记录保留历史含义，本批没有重写历史记录或改变工具审批参数指纹。

## 验证结果

最终关联回归 **1311 passed、6 skipped**，用时 46.34 秒；本批模型输入专项 **35 passed**。独立审查者运行模型输入、共享策略及上下文相关专项 **124 passed**，未发现本批剩余 P1/P2。受影响源码及专项测试 Ruff、`git diff --check` 通过。

- [测试日志](2026-11-02-model-input-tests.log)
- [JUnit 结果](2026-11-02-model-input-tests.xml)
- 专项代码：`backend/app/tests/unit/test_model_input_security.py`。

11 个真实 DeepSeek/ChatOpenAI 入口的内存 HTTP 样本在修复前均确认把合成 canary 放入请求正文，修复后均无 canary，且受信 Authorization Header 正常注入、正常流式输出仍有效。factory 的各能力分支和重试、schema 对象级模板、无效调用错误、协议字段总预算及 JSON 重复保护也有失败回归后修复的证据。

实际 checkpoint 用例使用小型 StateGraph + InMemorySaver 中断/恢复：模型收到历史与嵌套 TaskResult 的安全副本，原 checkpoint 历史及请求对象不变，ToolCallRequest 指纹仍一致。它不替代真实 PostgreSQL 历史存量或所有恢复分支的专项验收。关联回归另覆盖已有 API 审批/用户选择恢复、权限目录及任务上限、冻结目录、动态规划、RAG/MCP/搜索、记忆与压缩、Langfuse 及部署配置。

旧 Langfuse 集成测试原本要求模型请求仍含 password 明文，现改为断言实际请求无明文、公司事实和原状态保留；模型请求次数与 generation 观测次数仍一致，没有通过移除泄露断言绕过失败。

6 项跳过项仍为 5 项真实 PostgreSQL 迁移测试（未配置专用地址）和 1 项真实 DeepSeek 摘要质量评测（未显式启用）。所有本轮模型 HTTP、SDK 后台请求均由内存 transport 拦截，没有向真实模型发送合成凭证。

## 后续 TODO

1. 本层不清理原始 checkpoint/数据库，不保证历史资源或无来源文本的业务字段规则完整性；来源分类、后续用途、任意编码/改写及多轮记忆洗白仍待验收。
2. 模型输出流中新生成的凭证、HTTP 错误正文与异常链、全局日志及上游 chain/state 观测仍需统一检查。保护 generation 输入不能被当作所有采集出口已经安全。
3. 当前 DeepSeek 与公共适配以外，任意第三方客户端的直接私有调用、自定义 SDK 网络覆写/配置或未来模型能力需重新纳入接入清单；不通过公开原始 bound 对象宣称可隔离任意 Python 代码。
4. 真实模型抗注入、误报/漏报、生产 PostgreSQL 恢复和并发，以及已有候选资源归属/审批内容绑定等调查保持未完成。

当前仍保留全部总体安全 TODO，不把本批输入保护等同于完整 Agent 抗注入或全出口脱敏。
