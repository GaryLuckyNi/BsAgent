# SDK HTTP 读取预算接入计划

目标：修复当前 ChatOpenAI 原生调用与 MCP 官方适配器绕过直接 HTTP 读取预算的问题。属于既有安全控制维护，在 dev 执行，不提交/部署、不访问外部服务、不改业务 Agent。

设计：新增 `security/sdk_http.py`，通过 httpx Client/AsyncClient 子类在 send 处强制 stream=True，构造保留协议头/状态的有界响应流，复用上一批实体与解压计数。非2xx零正文读取、不跟随重定向；SSE在SDK行解析前做行预算。异步按绝对截止时间限制每次 anext，不能把 asyncio.timeout 跨原始流的 yield 留在调用任务上，避免早退后误取消无关工作。关闭由响应自身负责，有界关闭及取消控制语义保持。

ChatOpenAI 使用公开的 http_client/http_async_client 注入；传入客户端仅借用，wrapper关闭不关闭调用方资源。SDK默认客户端归模型拥有，补齐明确的异步关闭入口及应用退出调用。配置变更/跨事件循环的资源替换仍需独立确认，不用后台未跟踪任务处理关闭。

MCP 使用官方 httpx_client_factory，让发现、初始化和实际工具调用都经过相同边界；保留官方会话、工具转换与写入未知状态语义，不自行重写 MCP 协议。发现过程增加分页/工具累计上限与重复游标拒绝。SDK包装后的预算错误通过受限异常链检查识别，保持固定不可重试的响应限额码，不能变成新的放行或已确认未执行。

- [x] 新增 `tests/unit/test_sdk_http_budgets.py`：先红真实 OpenAI/LangChain invoke/ainvoke/stream/astream、错误/成功超限、正常JSON/压缩/SSE/工具调用对照；验证借用客户端未关闭、默认资源可关闭。
- [x] 在 HTTP 公共模块公开实体读取器/安全关闭契约，新增 SDK 客户端并接入 DeepSeek；检查SDK包装后的固定错误、不可重试、部分正文不发布。
- [x] MCP 官方工厂接入，验证真实 MCP 初始化/list_tools/call_tool 与JSON/SSE正常协议及超限/错误零读取；分页上限、重复游标和实际调用不能绕过。
- [x] 取消/早退/关闭故障与绝对期限专项；异步关闭/缓存/应用退出按原有资源清理契约测试，不遗留计时器影响后续任务。
- [x] 关联回归、静态检查、独立只读复核、验收与红队/TODO更新。SEC-09继续未完成：全run持久化预算、全局并发/连接、同步阻塞硬打断、其它控制面和前端HTTP等仍保留。

验证只用真实已安装SDK、httpx MockTransport与惰性流、临时数据库/会话，不以mock SDK方法代替协议集成证据。复跑专项：`.venv/bin/python -m pytest backend/app/tests/unit/test_sdk_http_budgets.py -q --tb=short`；关联使用上一批109文件清单追加本批及MCP原有专项。

2026-11-08最终证据：[验收记录](../../security/reports/2026-11-08-sdk-http-implementation.md)。SDK48通过（含后追加的默认HTTPX禁网夹具复跑）、112文件关联2194通过/6跳过、依赖/部署静态24通过；独立48SDK+74HTTP通过。所有权、吞错、关闭屏蔽及解析错误已按红测修复。首轮同步红测未完全隔离默认传输的限制如实保留；正常协议和响应客户端关闭有对照。默认native async改为每响应连接，不复用连接的生产代价未测；全run预算等总项仍未完成。
