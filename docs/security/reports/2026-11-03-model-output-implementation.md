# 模型输出与受管观测出口验收记录

日期：2026-11-03。仅本地实现和隔离验收；未提交、未部署、未运行数据库迁移，未修改独立 RAG 工程。

## 本批结论

用户已接受“完整接收后检查，再展示正文”。已接入的模型入口现在先汇总完整正文和推理，检查成功后才发布安全副本；失败流不发布半段正文。宿主生成的阶段进度仍可发送，模型生成的目标、计划、答案等内容延后。该取舍增加正文首次出现的等待时间。

复用现有共享凭证内核，覆盖已知 JSON 凭证、Cookie、私钥、较长密钥形状等。业务类别仍未配置，本批不声称完成业务脱敏或消除提示注入。

## 实施范围

| 边界 | 本批行为 |
| --- | --- |
| factory 与 DeepSeek 普通/流式/callback completion | 返回完整结果前保护；回调仅发布检查后的最终结果；截断重试不发布前一次半段 |
| ProtectedModel invoke/ainvoke/stream/astream | 检查完整消息、推理与元数据；原生流合并后检查工具参数；包含凭证的执行参数拒绝，不改成星号后执行 |
| SDK 输出回调 | 包装显式配置、绑定配置、继承配置和模型自身回调；支持同步和异步 handler；禁用原始逐 token/text/custom 回调，结束回调接收完整安全结果；错误回调不携带原异常或 partial response |
| Runnable 事件/日志流 | 等待完成后发布安全事件或最终日志快照；原始 stream chunk、streamed_output 逐片副本不外传 |
| 模型传输错误 | 固定错误码/消息，保留 HTTP 状态及超时阶段；不拼接 HTTP 错误正文、不保留原传输异常链；取消不转换为普通成功 |
| 受管 text/JSON 日志 | 格式化后的共享检查先于截断；复制 LogRecord；格式化失败使用固定占位，stream 写入故障不触发标准 stderr 原文转储 |
| 内部 Trace | 内存事件、artifact 和持久化入口保存安全副本；观测检查失败扣留内容，不改变工具副作用状态 |
| 应用受管 OTLP span exporter | 保护属性、名称、resource、scope、event、link、status 和 TraceState；异常正文/栈扣留；保留 trace/span ID、flags、remote 标记和时间 |
| Langfuse SDK | 属性统一 mask；SDK 无法通过属性 mask 修改的危险 event/status/resource/TraceState 整条 span 扣留；受管作用域以固定错误状态结束，不向 SDK 交付原异常栈 |

应用受管包装支持当前 ChatModel、Binding、Sequence、Parallel，以及外层重试/受保护 fallback。不能据此推定任意自定义 Runnable 内部私自创建的客户端都已接入。真实 SDK callback 内容处理只改副本，认证配置和工具调用身份保留。

正文累计缓冲限制为 2,000,000 字符、32,768 个事件；完整内容策略另限单字符串 1,000,000 字符、整体 2,000,000 字符、50,000 节点及深度。原生 chunk 按序列化体计入缓冲。以上不等于原始网络行大小、解码前响应、全任务内存/时间或并发预算已全部受控。

手写 HTTP 流记录 provider 实际首 token 时间及失败前接收计数，和用户看到正文的时间分开。SDK 原始逐 token 回调已禁用，不能继续把它当作实时 UI 正文或未检查内容的观测来源。

## 验收证据

- 83 个关联测试文件：**1695 passed、6 skipped**，见 [日志](2026-11-03-model-output-tests.log)、[JUnit](2026-11-03-model-output-tests.xml)及[文件清单](2026-11-03-model-output-test-files.txt)。
- 新专项独立运行：**58 passed**，见 [日志](2026-11-03-model-output专项-tests.log)及[JUnit](2026-11-03-model-output专项-tests.xml)。两组有重叠，不相加；正常工具 JSON 参数精确保留对照在关联运行后追加验收。
- 独立只读复核回归：**92 passed**，发现的 TraceState、SDK 配置/模型自身回调、日志格式故障旁路及 tuple 属性误判已修复并复验。
- 新文件和其余受影响核心文件 Ruff 检查通过。persistence 和 observability 仍有 6 条 HEAD 已存在的检查项（BLE001×4、B039、B010），未把它们算成本批新增或全项目静态检查通过。
- `git diff --check` 通过。所有模型和遥测请求使用内存 transport/内存 exporter，未调用真实模型或外部遥测服务。

跳过项：5 项真实 PostgreSQL 迁移测试没有配置专用地址；1 项真实 DeepSeek 上下文质量评测未显式启用。跳过不计通过。

专项覆盖跨字符 JSON/Cookie/PEM、传输失败零半段、HTTP/超时/畸形响应与异常链、原生执行参数拒绝、普通参数精确保留、日志正文和异常栈、格式错误 stderr、Trace 内存/持久化副本、真实 OTLP 导出、真实 Langfuse SDK、TraceState、正常 tuple resource、四种同步/异步 SDK 调用及四类 callback 配置、失败流 callback 不带 partial response。

既有流式测试按用户确认调整为“模型返回前无正文、返回后仍有正常公开结果”；首次失败未发布正文时允许既有重试/重新规划，断言不混入第一轮半段。摘要结果已在模型出口去除凭证时可直接验证安全摘要，栈/私有推理等仍走原摘要修复。历史窗口为零的集成场景改为显式设置测试条件，未修改产品当前窗口配置。

复跑关联集合：

```sh
xargs .venv/bin/python -m pytest -q --tb=short < docs/security/reports/2026-11-03-model-output-test-files.txt
```

## 留存 TODO

- SEC-05/07/10 总项仍不勾选：业务数据分类与接收方/用途规则、来源跟踪、任意改写/编码、真实多轮注入和误报/漏报评估未完成。
- Vision/OCR 独立请求、模型返回后业务拼接的链接/引用/卡片、其他客户端和独立出口逐一验收；不能以本批模型入口保护代替。
- 独立第三方 handler/exporter、metrics label、SDK 私有扩展和自定义 Runnable 隐藏客户端不自动受管；新增接入须验证检查先于回调或实际导出。
- 搜索等其他 provider 的原始异常链继续专项检查；受管观测拒绝原文不等于所有业务异常对象都已重建。
- 原始网络响应大小、整体任务预算、历史日志/数据库/checkpoint 清理、真实身份部署与跨租户/下游 ACL 验收继续保留。
- 实际输出延迟、缓冲峰值及部署环境 OTLP/Langfuse 配置仍需线上前验收，不能用本地确定性测试推定真实模型抗注入能力。
