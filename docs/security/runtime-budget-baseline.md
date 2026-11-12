# 当前运行预算与失控保护基线

初始日期：2026-11-06；任务账本复核：2026-11-08。范围：当前工作区源码与本地测试，不代表生产容量或真实Redis/PostgreSQL多进程验收。对应SEC-09；入口、直接HTTP/SDK及当前run基础资源账本已分批接入，费用/并发/独立job等仍未完成。

## 已存在的限制与适用范围

数值为 Settings 默认值或源码常量，实际部署值须另行核对。入口限额本批改为读取实际 app 配置；其它模块仍可能读取进程全局 settings，不能据此推定全部配置已支持每 app 注入。

| 资源/位置 | 当前限制 | 能保证的范围与缺口 |
| --- | --- | --- |
| 全run：[run_budget](../../backend/app/security/run_budget.py)、[HTTP计量](../../backend/app/security/run_budget_http.py) | 模型32次、工具64次、HTTP512信用、请求正文16MiB、原始/解压各128MiB信用、1800秒；均可配置 | 首次创建冻结，审批等待计时，恢复不补发；独立短事务扣减/授信，越额持久封闭，旧缺账本拒绝。仅当前受管模型/工具/HTTP；不等于token/费用、事件/存储、独立job或全局并发总额 |
| Agent HTTP 入口：[rate_limit](../../backend/app/operations/rate_limit.py)、[sessions](../../backend/app/api/sessions.py)、[approvals](../../backend/app/api/approvals.py)、[ag_ui](../../backend/app/api/ag_ui.py) | 默认20次/分钟，配置1～10000；tenant/user 摘要与统一 agent_invocation 桶；生产 Redis 必需且 fail-closed | 同步、普通 SSE、protocol POST、AG-UI 与审批 decision 共用入口额度，AG-UI 内部调用不重复扣除。该桶计请求尝试，不是工具总次数、并发数、每租户总额或整任务费用；固定窗口不能当作任意滚动60秒的平滑限额 |
| 图片上传：[assets](../../backend/app/api/assets.py)、[config](../../backend/app/core/config.py) | 默认10次/分钟；单图10MiB、2000万像素；单消息默认4个附件（1～16） | 上传/解码与附件边界，不是服务总存储、所有请求体字节、远端解压字节或每用户图片生命周期总额度 |
| Graph：[runtime](../../backend/app/graph/runtime.py) | recursion_limit 默认128，配置8～256 | 一次图执行步数；恢复、重新编译/运行与节点内部循环不等于同一持久化全任务预算 |
| Planner：[policy](../../backend/app/graph/planning/policy.py)、[request_flow](../../backend/app/graph/planning/request_flow.py)、[planner_agent](../../backend/app/graph/nodes/planner_agent.py) | MAX_PLANNER_ITERATIONS=2；实际迭代取 min(settings.planner_max_iterations, 2)，配置默认10；MAX_REPLAN_REQUESTS=1；MAX_PLANNER_FEEDBACKS=24 | 不应把配置10描述成实际可执行10轮；不等于整个 run 所有 LLM 调用/输入输出 token/修复请求总预算 |
| web_search：[call_budget](../../backend/app/tools/call_budget.py)、[gateway](../../backend/app/tools/execution_gateway.py)、[execution](../../backend/app/conversations/execution.py) | MAX_WEB_SEARCH_TASKS=6；内存 Lock 预留，恢复从既有调用重建；durable 路径在 run 锁下预留 receipt | 真实 web_search 派发受该局部预算约束；同 call ID 的 durable 复用核对绑定/指纹/结果摘要，未知调用阻断。其它 RAG/MCP/替代工具没有通用同类总额；不能只改工具名就声称仍共享搜索预算 |
| 工具发现：[config](../../backend/app/core/config.py)中的 tool_search_max_calls / tool_search_max_disclosed_tools | 默认4次发现（1～16）、披露16个工具（1～64） | 这是发现目录的限制，与 web_search 网络检索额度不同；不等于实际工具执行次数 |
| 节点重试：[runtime](../../backend/app/graph/runtime.py) | operational policy 最多3次尝试，退避0.2秒、倍数2、上限2秒；tool_agent/permission_recovery 不自动重试整个节点；synthesizer 仅未发布且可重试错误才重试 | 保护局部重试和副作用语义；SDK 内部重试、worker 重投及替代工具仍需按同 run 预算核算，不能用局部次数相乘替代总额 |
| HTTP/模型超时：[deepseek](../../backend/app/llm/deepseek.py)、[RAG](../../backend/app/tools/rag_search.py)、[config](../../backend/app/core/config.py) | LLM默认60秒（0～300且大于0）；搜索30秒（0～120且大于0）；RAG/MCP默认30秒，当前配置尚无同类有限正数约束；OCR/Vision60秒（0～300且大于0） | 传给 HTTP/SDK 的超时不能当作整个任务 wall-clock 截止时间；直接模型/搜索/RAG/OCR/Vision HTTP 已接入本节下方的响应预算；同步阻塞、未接入的自建 SDK 和整体 run 期限仍缺硬约束。Gateway 对任意 executor 没有通用外层超时 |
| OCR/Vision 并发/熔断：[factory](../../backend/app/vision/factory.py)、[resilience](../../backend/app/vision/resilience.py) | guard 每实例 BoundedSemaphore 默认4（1～64），等待0.5秒（0～30）；熔断失败阈值5、恢复60秒，状态可用 Redis | 信号量是本进程/guard 实例限制，Redis 熔断状态不等于分布式并发额度；没有通用 LLM/MCP/搜索并发上限 |
| 正文/内容检查：[tool_content](../../backend/app/security/tool_content.py)、[model_output](../../backend/app/security/model_output.py) | 默认工具策略单字符串64000字符、累计100万字符、20000节点、深32；模型输出策略单字符串100万、累计200万、50000节点 | 是解析后内容检查限制，多个适配点可覆盖；已接入直接 HTTP 另有实体字节预算，但 JSON 对象图、未知 SDK 收集与未接入传输可能先分配内存，不能当作进程峰值内存硬上限 |
| 持久化重放：[protocol_replay](../../backend/app/security/protocol_replay.py) | 每次重建最多20000事件、200万正文字符，分页200；完整重建后检查 | 不是整个 run 的 DB 行/事件写入总额；不断 GET 重建和每帧授权查询仍会消耗数据库和连接资源 |
| 停止与回执：[durable](../../backend/app/conversations/durable.py)、[execution](../../backend/app/conversations/execution.py)、[tasks](../../backend/app/conversations/tasks.py) | owner/lease、取消检查、调用回执；Celery execute_turn 显式无 hard/soft time limit | 在途结果先保存、未知副作用禁止盲重试；不能硬杀后把操作标为未执行。尚无整个 run 绝对截止时间和统一取消/未知结果预算契约 |
| 压缩/补偿：[tasks](../../backend/app/context_compaction/tasks.py)、[synchronizer](../../backend/app/memory/synchronizer.py) | 压缩 task max_retries=3，退避最大30秒；记忆同步单批最多100，机会补偿5 | 单次处理有界；后台队列积压、长期失败重投、模型费用和任务整体期限仍需统一核算 |

## 直接 HTTP 响应预算（2026-11-07）

[公共模块](../../backend/app/security/http_response.py)已接入直接 DeepSeek JSON/同步异步 SSE、两个搜索协议、内置 RAG HTTP、OCR 与 Vision。每响应原始 HTTP 实体/解压后各8MiB，SSE 每行256KiB；实际计数，不信任 Content-Length。非2xx零正文读取、不跟随3xx；gzip/zlib-deflate/raw-deflate 有限解压，未支持编码、叠加/截断/尾随数据固定拒绝。异步请求采用实际客户端 timeout_seconds 作为发送到读取的绝对时限，另有最多1秒关闭宽限；同步仅在读写返回与完成边界复查，底层阻塞不能强行打断。

取消与非Exception控制信号在关闭故障后仍保留，不转换成普通可继续错误；EOF自动关闭、深SSE JSON与RAG格式错误均固定处理。验收见 [第十一批记录](reports/2026-11-07-http-response-implementation.md)。原始实体不含头/分块封装/TLS，可能已读入一个越界块；注入方提前缓冲、JSON结构峰值、当前SDK已由下一节分批覆盖；其它自建客户端/控制面/前端HTTP与整个run预算仍不在保证内。

## 当前模型/MCP SDK 预算（2026-11-08）

[SDK适配层](../../backend/app/security/sdk_http.py)已通过 ChatOpenAI 的 http_client/http_async_client 和 MCP 的 httpx_client_factory 接入相同响应预算，保留真实协议/状态/元数据，不全局替换HTTPX。异步读取每次anext沿用同一绝对期限，不在yield外遗留计时上下文。MCP host scope仅接收本地受控传输的预算信号，覆盖SDK吞掉读取、关闭及发送错误的路径；拒绝不能依据远端同名字符串，实际调用不虚标未执行。

MCP单次发现累计64页、2048工具及8MiB序列化定义，重复cursor拒绝，失败不返回半份目录；这是目录预算，与tool_search发现次数和实际调用总数不同。默认原生模型异步HTTP每响应拥有/关闭客户端以避免跨asyncio.run闲置池，代价是不复用连接；显式借用的客户端及MCP会话池仍由原所有者管理。应用退出await模型关闭，sync退休拒绝新准入，在途响应自身收尾。

证据见 [第十二批记录](reports/2026-11-08-sdk-http-implementation.md)。这不是全模型/工具总调用、总token/费用、跨恢复/替代工具持久化账本或分布式并发限制；生产连接建立成本、其它宿主自建SDK、控制面/前端读取、同步阻塞与JSON结构峰值仍待验证。

## 本批修复与验收方法

生产配置拒绝 rate_limit_fail_closed=False；factory 对未验证 model_copy 也强制生产 fail-closed 并拒绝禁用 backend。配置要求 Redis 而服务缺失时始终返回固定503；生产或启用 fail-closed 时，存储失败或畸形 hit/window 结果同样固定503，未进入受保护操作，不保留原始连接异常链。非生产显式 fail-open 仍保留故障放行兼容路径。429 的 Retry-After 限1～60秒。

受信 policy 可声明白名单配置字段，依赖读取实际 app 配置；静态自定义 policy 保持原阈值。审批 decision HTTP 入口复用 agent 桶，AG-UI 内部直接函数调用沿用外层扣次。业务 JSON 中关闭 backend 或 fail-closed 的字段不影响依赖策略。Settings 默认错误文本不再回显输入值；这不等于显式调用 ValidationError.errors() 的结构化内容也已脱敏。

使用真实 limits 内存后端、多个 FastAPI app 与并发 TestClient，验证同主体共享额度、不同租户/用户隔离、正常放行、静态配置与上传配置、超限不查审批资源。合成故障后端验证固定错误。证据见 [本批验收记录](reports/2026-11-06-entry-budget-implementation.md)；不把共享内存测试称为真实 Redis 多进程测试。

## 下一步必须保持的预算契约与 TODO

1. 当前受管模型/工具/HTTP次数、请求正文、实体/解压信用和期限已进入同一持久化账本；继续补输入输出token、费用定价/可信usage、事件写入/存储及独立job预算，不能把当前字节额度当计费保证。
2. 继续覆盖其它自建客户端、控制面和前端授权错误 response.clone().json()，实现多轮HTTP认证/hook的逐次计量后再开放（当前run内拒绝）；补齐headers/JSON结构/解析CPU与同步阻塞硬期限。保持固定错误及正常响应对照。
3. 每用户/租户/服务的在途任务、订阅连接与 provider 并发，跨进程共享和退出释放；给流式授权查询与重建成本设定上限，避免“为了安全每帧查库”本身成为拒绝服务入口。
4. 保持本批耗尽封闭、过期停止、清理预留与回执确定性契约；继续验证真实未决远端调用和故障补偿。未派发必须不派发，已派发不能抹去；未知写入需核实，不能通用自动重试。
5. 明确生产部署数值、真实 Redis/PostgreSQL 并发/恢复验证、独立安全审计与容量/正常任务延迟门槛。当前 SEC-09 继续未勾选。

## 持久化任务预算补充（2026-11-08）

以上旧批次中“尚无全任务预算”的描述是当时边界；当前实现与验证见 [第十三批记录](reports/2026-11-08-run-budget-implementation.md)。已有web_search局部6次限制与新全run64次工具额度同时生效，换工具不重置后者。响应信用按最多64KiB非退款预留，不是精确收发账单；压缩膨胀在授信前限制输出窗口。多轮Digest/OAuth认证及HTTPX事件hook可能在send内部重发/预读，当前绑定run时失败关闭，None/Basic认证及普通授权头仍支持。

配置为 `BS_AGENT_RUN_BUDGET_MODEL_CALLS/TOOL_CALLS/HTTP_REQUESTS/REQUEST_BYTES/RESPONSE_WIRE_BYTES/RESPONSE_DECODED_BYTES/LIFETIME_SECONDS`，开发和生产样例已补，生产Compose向后端及worker传递相同配置。修改只影响新建run，旧任务保留冻结额度与期限；0019迁移不自动回填历史任务，部署尚未执行。
