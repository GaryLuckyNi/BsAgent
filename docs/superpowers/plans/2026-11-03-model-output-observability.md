# 模型输出、错误正文与受管观测出口实施计划

目标：按用户确认，完整接收模型正文后再检查/发布，阻止已知凭证通过跨片段输出、HTTP错误正文、受管日志/Trace/Langfuse/OTLP扩散；不定义业务敏感类别。

架构：独立 model_output 和 observation 安全适配复用共享凭证内核。普通结果和流式结果统一保护；流式正文/推理先有界汇总，成功完成后才向调用方/回调发布安全副本，失败不发布半段。宿主阶段进度仍可发送，模型生成的进度字段会延后。tool call 的执行参数含受限材料时拒绝，不静默改成星号后执行。受信认证 Header 正常注入。

- [x] 先复现：跨chunk JSON/Cookie/PEM、失败后的半段正文、普通/SDK/流式HTTP错误及异常链、JSON/text日志与异常栈、Trace与实际OTLP/Langfuse导出。
- [x] 新增统一固定模型错误码/消息，保留status/timeout phase，不读响应错误正文、不保留原始传输异常链；取消继续传播。
- [x] 模型结果/流式缓冲接入factory、DeepSeek公开方法和ProtectedModel，保留响应状态/统计以及实际调用身份，明确正文延迟与provider首token统计的区别。
- [x] 共享观测副本接入受管日志、内部Trace内存/持久化、Langfuse正文及应用OTLP span exporter。观测故障丢弃正文，不影响已经执行的工具；原始对象不改写。
- [x] 对SDK无法通过属性mask修改的异常事件/状态，保守拒绝不安全Langfuse span导出；不声称控制第三方独立handler、metrics label和所有外部SDK。
- [x] 关联回归、独立复核，记录缓冲上限、跳过项和剩余出口TODO。

范围限制：不识别任意未知业务秘密，不验证所有编码/多轮注入，不清理历史数据，不保证原始网络行读取/整体任务预算。模型响应之外的业务拼接链接、Vision/OCR等独立出口仍须专项验收。任何副作用状态不得因内容处理被改为未执行。

验收证据：[实施记录](../../security/reports/2026-11-03-model-output-implementation.md)。关联1695 passed、6 skipped；专项58 passed；独立复核92 passed，集合有重叠不相加。总安全 TODO 保持未完成。
