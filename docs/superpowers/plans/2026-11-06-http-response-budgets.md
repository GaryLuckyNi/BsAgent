# HTTP 响应读取预算维护计划

目标：修复已接入模型、搜索、RAG、OCR/Vision HTTP 成功/失败响应在检查前无界读取的问题。沿用已批准的公共适配层安全方向及预算基线，在 dev 维护现有客户端；不创建工作树、不提交、部署或访问外部服务。

设计：增加独立 `security/http_response.py`，复用 httpx 的 stream=True，在 JSON 解析前分别累计原始与解压后字节；默认各8MiB。请求声明 Accept-Encoding: identity，但仍对服务端返回的 gzip/deflate 做限量解压，拒绝其它编码、叠加编码、截断压缩流及压缩尾随数据。错误/重定向响应不读取正文、不跟随跳转；返回剥离原始连接信息的有界响应副本供既有状态/JSON 逻辑使用。Content-Length 仅为提前拒绝条件，实际流计数始终生效。

异步请求从发送到读取用 asyncio 绝对超时，并在解压/完成边界复查时间；同步请求采用每次读取前后复查，底层阻塞仍受 httpx read timeout 约束，不能宣称硬打断同步阻塞。关闭连接、取消、错误映射不泄露原请求凭证/异常链；超限不自动改成可重试。注入客户端已预缓冲的数据只能检查现有长度，不追溯保证它此前的分配安全。

模型 HTTP SSE 同时接入累计字节及每行上限256KiB，防止 iter_lines 在无换行输入上无界缓冲；正文仍在完整内容检查之后才发布。第三方 ChatOpenAI/MCP SDK 隐藏传输、控制面 HTTP、前端错误解析与通用全任务预算仍在总清单中，不能由本批推定完成。

## 任务与验证

- [x] 写 `backend/app/tests/unit/test_http_response_budgets.py` 先失败：真实 httpx 自定义流，无 Content-Length/谎报长度、成功/错误、字节上限、gzip/deflate 炸弹、编码/截断、异步绝对期限、同步完成后过期、关闭及正常 JSON/UTF-8 对照。没有新模块时先确认导入失败，再逐条驱动公共模块实现。
- [x] 接入 `llm/deepseek.py` 的直接 JSON/SSE、`search/deepseek.py`、`search/deepseek_api.py`、`tools/rag_search.py`、`vision/client.py`、`vision/ocr_client.py`。写请求级失败复现：无界流未被提前关闭；错误正文不应读取；gzip/JSON 正常结果保留。运行既有协议/内容/授权测试，固定错误保持原状态语义。
- [x] 公共流/解压模块检查错误无 canary/cause/context，异步取消与超限均关闭资源；SSE 无换行及跨 UTF-8 分段正常对照，不把网络字节限制等同于正文凭证检查。
- [x] 执行专项与上一批108文件关联集合、新增测试、静态检查、独立只读复核；更新验收报告、红队补充、预算基线和 TODO。保持 SEC-09 及全部未完成总项未勾选。

复跑命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_http_response_budgets.py -q --tb=short`。协议关联使用上一批 manifest 加新增文件；每次记录真实通过/跳过，不把真实外部服务与模型测试替身混同。

2026-11-07最终证据：[验收记录](../../security/reports/2026-11-07-http-response-implementation.md)。专项74通过，109文件关联2093通过/6跳过，独立只读复核74通过，发现的兼容/EOF/深SSE/取消组合问题已先红后修。SEC-09与未覆盖SDK/整体run/同步阻塞等总项继续未完成。
