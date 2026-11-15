# 第十二批：模型与 MCP SDK 的 HTTP 预算接入

开始日期：2026-11-07；最终复核：2026-11-08。范围：dev 工作区的现有 SDK 调用边界维护。未提交、迁移或部署；没有修改独立 RAG 工程或调用真实业务工具。

## 问题与接入

上一批直接 HTTP 预算没有自动覆盖 ChatOpenAI 原生模型调用及 MCP 官方适配器内部创建的客户端。本批通过公开注入点接入 [SDK 客户端](../../../backend/app/security/sdk_http.py)，不改 SDK 协议解析，也不在进程中全局替换 HTTPX。

| 路径 | 最终行为 |
| --- | --- |
| DeepSeek 构造的 ChatOpenAI | 同步/异步客户端均受控；原生 invoke/ainvoke/stream/astream、工具绑定及现有回调共用该实际传输路径 |
| MCP 官方 Streamable HTTP | 使用 httpx_client_factory；真实初始化、JSON/SSE 发现、实际 tools/call 都经过同一边界，保持官方会话及工具转换 |
| 实体/解压/行预算 | 复用公共 HttpResponseReader：单响应原始实体及解压后各8MiB，SSE单行256KiB；非2xx不读正文，不跟随3xx |
| 协议元数据 | 保留协议状态、Content-Type、请求ID等；解压后的成功响应移除原压缩/长度/分块头，不把原传输对象交给公开错误 |
| 异步等待 | 发送阶段绝对期限，读取按同一个截止时间限制每次 anext；不把 asyncio.timeout 上下文跨流的 yield 留在调用任务上 |
| 关闭与取消 | 原始流 EOF、显式 aclose 和 finally 都使用有界关闭；MCP 内部 task group 收到完整 RPC 后的取消不抢掉关闭宽限，外部 asyncio 取消仍保持 CancelledError |

同步底层阻塞仍不能硬打断。自定义 transport、响应钩子若提前自行缓冲，不能追溯约束此前的分配。实体预算不含头、TLS、分块封装，也不是 RSS/JSON对象图或整个run的总量保证。

## SDK 吞错、固定错误及副作用

真实 MCP SDK 会捕获读取异常并送入内部通道，部分路径最终只表现成超时；SSE还可能在 RPC 已交付后吞掉关闭故障。不能仅检查异常cause链，也不能相信远端返回的同名错误字符串。

为每次本地发现/调用建立短生命周期的 host scope。只有本地受控传输产生的 HttpResponseBudgetError 能设置该 scope 的信号；发送超时、实体/行限制及关闭错误均覆盖。信号与原 RPC 竞争，拒绝后取消并等待其任务收尾，scope在finally重置。工具正文或普通State不能制造此标记。

MCP 超限返回固定 mcp_response_limit_exceeded，发现失败也不发布半份目录。实际工具调用仍保留 uncertain，不能因响应读取失败就标记 not_started，不能据此盲重放写入。每次发现累计最多64页、2048个工具及8MiB序列化定义；重复游标直接拒绝，页间不重置额度。

OpenAI SDK 包装错误用最多32个异常对象的有限链/组检查识别真实预算错误，保持预算错误优先，状态/连接/超时分类不变。JSON语法、深度、非法编码、SDK错误对象及响应类型错误归一为 llm_invalid_response；APIError 的服务器文字不直接发布。模型消息保护与选项检查在提供方调用捕获边界之前，安全拒绝不被当作普通响应错误。默认SDK max_retries=0，模型响应预算错误仍为已有不可重试终止码；这不是所有宿主自定义重试或全run预算已完成的证明。

## 资源所有权与行为变化

显式传入的同步/异步 HTTPX 客户端只借用，模型关闭不关闭调用方的连接池。

默认原生异步客户端采用 **每响应拥有连接**：非流式读完后立即关闭，流式随 EOF/关闭释放；门面不持有闲置的跨事件循环连接池。同步close/配置换代可同步退休门面并拒绝新请求，已有在途响应继续由自身负责收尾。正常工具调用在两次独立 asyncio.run 中有成功及资源释放对照。

代价是默认原生异步请求不复用连接；本批没有生产吞吐/连接建立延迟基准。显式借用客户端和 MCP 的会话客户端仍可复用其自有池。同步池保持原有复用方式。新增 DeepSeek.aclose、factory.aclose_llm_client，应用退出明确await，后续其它资源仍按既有失败收尾契约关闭，不使用未跟踪后台任务退休连接池。

AnyIO shield 直接依赖已在 pyproject.toml 声明，安装/锁定版本仍为4.14.2，没有升级包；pip check 及声明/锁定一致性检查通过，只有既有pip缓存目录不可写提示。

## 先失败再修复与最终证据

主要红测：

- [模块缺失](2026-11-08-sdk-http-import-red.log)、[原生调用边界](2026-11-08-sdk-http-native-red.log)；后者初始4失败/2通过，暴露同步客户端未注入、异步无界读取/缺少关闭入口。
- [真实 MCP JSON/SSE](2026-11-08-sdk-http-mcp-red.log)及 [SDK吞掉预算异常](2026-11-08-sdk-http-mcp-scope-red.log)；正常协议对照成功，发现/调用超限先失败。
- [累计发现限制](2026-11-08-sdk-http-mcp-discovery-red.log)、[关闭故障](2026-11-08-sdk-http-mcp-close-red.log)、[发送期限被吞](2026-11-08-sdk-http-header-red.log)。关闭超时的独立复现推动将shield移到外层关闭等待边界，不能只保护已被wait_for创建的子任务。
- [异步关闭入口](2026-11-08-sdk-http-lifecycle-red.log)、[缓存退休/响应所有权](2026-11-08-sdk-http-retirement-red.log)。
- [SDK解析错误](2026-11-08-sdk-http-parse-red.log)、[错误对象/非法UTF-8/SSE APIError](2026-11-08-sdk-http-api-error-red.log)。均有固定码、无原始正文/cause/context的最终断言。

**隔离纠正：首轮同步SDK红测未完全隔离默认传输。** 原代码忽略同步注入客户端，记录中的Mock请求数为0；因此不能从那次执行推定网络零访问。测试代码显式只传入合成模型API key和公开问题，没有传入真实模型API key。之后新增本批autouse夹具，直接禁止默认 HTTPTransport/AsyncHTTPTransport 的网络发送；最终48项SDK专项在该夹具下重新执行，所有协议走MockTransport。保留原始过程记录，不将其改写成离线成功证据。

- 最终SDK专项 **48 passed**：[日志](2026-11-08-sdk-http-unit-tests.log)、[JUnit](2026-11-08-sdk-http-unit-tests.xml)。最终禁止默认HTTPX网络传输夹具下复跑的结果以此为准。
- 最终关联 **112文件、2194 passed、6 skipped**：[日志](2026-11-08-sdk-http-tests.log)、[JUnit](2026-11-08-sdk-http-tests.xml)、[文件清单](2026-11-08-sdk-http-test-files.txt)。该集合在上述测试隔离夹具追加前、相同生产代码下执行；随后48项复跑验证隔离。关联不称为整个仓库全量测试，重叠统计不相加。
- 初次关联2188通过的 [日志](2026-11-08-sdk-http-before-parse-tests.log)/[JUnit](2026-11-08-sdk-http-before-parse-tests.xml)只作过程证据，之后发现的6项SDK解析缺口已补红测并修复，不以前次绿灯替代最终验收。
- 包声明更新后部署/配置静态专项 **24 passed**：[日志](2026-11-08-sdk-http-dependency-tests.log)、[JUnit](2026-11-08-sdk-http-dependency-tests.xml)。没有重新构建镜像或部署。
- 6个跳过分别为5项缺少真实PostgreSQL地址的迁移、1项未启用的真实DeepSeek质量测试，不计通过。真实模型、真实MCP服务、TCP容量和生产环境仍没有本批验收证据。
- 独立只读复核发现并推动修复闲置异步池、MCP关闭/发送漏记、SDK解析错误；最终48SDK+74HTTP共122项通过，所审范围未发现剩余P1/P2。后追加的隔离夹具最终专项亦重新通过。
- 本批所有改动Python模块/测试Ruff通过；无前端修改。git diff --check 与本批文档本地链接检查通过。

两个旧测试替身同步修正：MCP连接字典断言接受并确认新增官方工厂；生产维护lifecycle测试提供惰性Redis连接池/redis配置，符合上一批fail-closed规则，并断言异步模型关闭确已await，没有放松生产检查。

已安装并复核的版本：HTTPX0.28.1、OpenAI2.54.0、langchain-openai1.2.2、langchain-mcp-adapters0.3.2、MCP1.28.1、AnyIO4.14.2。升级后需重新执行真实协议与取消/关闭用例。

复跑：

```sh
.venv/bin/python -m pytest backend/app/tests/unit/test_sdk_http_budgets.py -q --tb=short
xargs .venv/bin/python -m pytest -q --tb=short < docs/security/reports/2026-11-08-sdk-http-test-files.txt
```

## 原有总项继续保留

当前构造的模型与MCP SDK路径已接入，不等于任意宿主另行创建的客户端或所有SDK都受控。Admin/Skill控制面、前端HTTP、JSON结构/CPU/峰值内存、原始像素外发、同步阻塞硬期限仍需处理。全run持久化总量、跨恢复/替代工具预算、分布式并发/订阅连接、独立审计、业务分类、RAG下游服务身份与文档ACL、后台记忆/压缩授权/来源、历史清理、真实注入及真实部署继续按 [TODO](../TODO.md)验收；SEC-09等总项不勾选。
