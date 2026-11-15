# 第十一批：HTTP 响应读取、解压与流式行预算验收

开始日期：2026-11-06；最终验证：2026-11-07。范围：dev 工作区的 BsAgent 公共 HTTP 读取边界。未提交、迁移或部署，未访问真实外部模型、RAG、OCR、Vision 或 Redis；没有修改独立 RAG 工程。

## 问题与结果

原有直接 HTTP JSON 客户端使用 post/send 的默认完整读取，检查发生在响应已缓冲之后。错误正文虽不展示，仍会完整读取；模型 iter_lines/aiter_lines 在无换行的输入上也会先积累大行。已有内容字符/节点限制不能覆盖这些资源消耗。

新增 [公共读取模块](../../../backend/app/security/http_response.py)，接入如下路径：

| 路径 | 当前接入 |
| --- | --- |
| DeepSeek 直接 HTTP | JSON completion、同步/异步 SSE；第三方 ChatOpenAI 原生绑定/回调的隐藏传输不由此推定覆盖 |
| 两个搜索协议 | 独立 Search API、Anthropic 风格协议的每个请求及续轮 |
| 内置 rag_search | BsAgent 调用 localhost:8011 的 HTTP 适配，不改服务端 ACL/认证 |
| OCR/Vision | PaddleOCR multipart 请求与兼容 OpenAI 的 Vision JSON 请求 |

每个响应接受的原始 HTTP 实体字节与解压后字节默认各不超过 **8MiB**；模型 SSE 每行不超过 **256KiB**。用 stream=True 读取并实际累计，不能靠缺失或谎报的 Content-Length 绕过。非2xx响应不读取正文，3xx也不跟随；原有服务状态分类和 RAG 404 无证据结果保持。业务契约不接受的2xx仍可能先有界读取再拒绝，不混称零正文读取。

Accept-Encoding 固定请求 identity；若上游仍发 gzip/deflate，按剩余输出预算限制每次解压。deflate 包括 zlib 包装及首次失败时的 raw DEFLATE 回退，单字节分片也有正常对照。拒绝不支持的 br/zstd、叠加编码、截断压缩流及尾随数据（含多 member gzip）；不进行无界 flush，也不在超限时静默截断成成功资料。

异步请求在发送到正文读取期间采用绝对超时，连续小片段无法只靠刷新 read timeout 无限延长。同步请求在发送返回、每个读取块及完成边界复查期限；底层同步阻塞不能被该模块硬打断。关闭另有最多1秒的异步宽限；任意不配合取消的自定义 transport/close 实现不在可强行终止保证内。

限额/编码/关闭故障映射固定服务错误，不保留上游 URL、凭证或异常链。LLM 响应限额错误为不可重试的终止错误；搜索超限为不可重试，绝对期限仍按既有 timeout 语义处理。用户取消、GeneratorExit 和非 Exception 的控制信号优先保留；取消期间关闭失败仅记录固定原因，不转成可继续运行的普通错误。

RAG 无效 JSON/契约及过深 JSON 改为固定 rag_response_invalid；模型过深 SSE JSON 改为固定 llm_invalid_response。即使此前收到安全正文，也必须完整接收/检查后发布，失败时不发布该部分正文。图片文本和工具成功/失败内容仍使用既有共享内容策略，读取预算不替代凭证与提示注入检查。

## 先失败再修复的证据

- 新公共模块导入缺失，确认 [首轮失败](2026-11-07-http-response-red.log)。
- 实际客户端请求边界 **14失败、2通过**，复现超大成功响应完整读取及错误正文未跳过；两种 SSE 错误响应原本已经零读取，正常计为通过，见 [客户端红测](2026-11-07-http-response-clients-red.log)。
- 关闭失败和 RAG 原始校验错误 **6失败**，见 [红测](2026-11-07-http-response-cleanup-red.log)；进一步发现 HTTPX EOF 自动关闭在显式 finally 之前抛出，已在原始迭代消费边界覆盖。
- 独立复核指出 raw DEFLATE 兼容问题，增加正常和膨胀负例先出现 **4失败**，见 [红测](2026-11-07-http-response-raw-deflate-red.log)。
- 超深 SSE JSON 同步/异步 **2失败**，见 [红测](2026-11-07-http-response-deep-sse-red.log)；取消叠加关闭错误/超时 **2失败**，见 [红测](2026-11-07-http-response-cancel-red.log)。均有精确错误、资源关闭和不发布部分正文的最终断言。

既有两项测试按分层契约更新：最终模型元数据检查用例只在测试内提高行预算，继续精确断言 ModelOutputRejected，确保更早的网络限制没有遮蔽原来的检查；RAG 缺失必需 results 的用例改为精确断言固定 RagSearchError/code/502，不再暴露原 ValidationError。没有放宽拒绝断言为任意异常。

## 最终验证

- HTTP 专项 **74 passed**：[日志](2026-11-07-http-response-unit-tests.log)、[JUnit](2026-11-07-http-response-unit-tests.xml)。
- 关联集合 **109文件、2093 passed、6 skipped**：[日志](2026-11-07-http-response-tests.log)、[JUnit](2026-11-07-http-response-tests.xml)、[清单](2026-11-07-http-response-test-files.txt)。5项真实 PostgreSQL 迁移地址缺失，1项真实 DeepSeek 质量评测未启用；跳过不计通过，也不把该集合称为整个仓库全量测试。专项与关联统计重叠，不相加。
- 使用真实 httpx Client/AsyncClient 与自定义 SyncByteStream/AsyncByteStream、MockTransport，实际走 stream=True/原始迭代与关闭，不用预缓冲响应证明网络上限；正常既有协议测试另覆盖预缓冲测试替身。没有真实 TCP/公网、模型或生产容量验证。
- 独立只读复核发现并推动修复上述兼容、EOF、深 JSON 和取消组合问题；最终独立复验74项通过，取消仍保持 CancelledError/task.cancelled()，所审范围未发现剩余P1/P2；不将此前72项通过当作取消组合已修复的证据。
- 本批公共模块、所有改动消费者/错误映射及专项/相关旧测试 Ruff 通过；无前端变更。git diff --check 与本批文档本地链接核对通过。

复跑：

```sh
.venv/bin/python -m pytest backend/app/tests/unit/test_http_response_budgets.py -q --tb=short
xargs .venv/bin/python -m pytest -q --tb=short < docs/security/reports/2026-11-07-http-response-test-files.txt
```

## 保留边界

1. 这是单响应实体预算，不含 HTTP 头、分块封装、TLS 或整个 run 的带宽/费用。为发现超限，底层可能已经读入一个越界块；自定义 transport/响应钩子若在返回前自行预缓冲，不能追溯防止其分配。8MiB 不是进程 RSS 或 JSON 对象图的峰值内存保证，结构节点数/解析 CPU 仍需统一预算。
2. 同步底层阻塞与故障 close 无法靠边界计时强行打断；异步超时与关闭依赖 transport 配合取消。没有以硬杀进程并抹去已派发调用的方式伪装取消成功。
3. 第三方 ChatOpenAI/原生绑定/模型自身 SDK、MCP SDK、Admin/Skill 控制面 HTTP、前端 response.clone().json() 以及其它未接入客户端仍需逐个适配。当前内容安全入口已保护 SDK 内容，不等于它们的原始网络读取预算已完成。
4. 全 run 持久化预算、跨恢复/替代工具的总次/token/费用/期限、分布式并发、订阅连接、事件写入与独立审计仍未完成；[SEC-09](../TODO.md)继续未勾选。
5. 业务数据类别未定义、RAG 下游服务身份/文档 ACL、后台记忆/压缩的可信委派与来源、历史原文清理、真实多轮/多模态注入及实际部署继续按原有 TODO 验收。
