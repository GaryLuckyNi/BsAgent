# 本 session 安全模块延迟复核

日期：2026-11-10。结论：存在已确认的正文发布延迟，以及可优化的重复扫描和高频数据库访问。本轮仅检查、计时和记录，没有修改应用实现或降低安全控制；没有访问真实模型、RAG、数据库或身份服务。

## 结果与优先级

| 优先级 | 操作 | 影响与本地证据 |
| --- | --- | --- |
| 已确认的产品取舍 | 模型正文完整缓冲后检查，再发布 | 首字等待完整生成结束。假模型每30ms产生一片、共5片：直接首片30.14ms，保护后首片156.56ms且包含完整20字符。这不是规则扫描慢导致的全部延迟；此前用户已接受检查前不展示正文 |
| 优先优化 | 响应字节预算以64KiB信用频繁访问同步数据库 | 原始与解码后各1MiB：128次SELECT、64次UPDATE、32次COMMIT，本地88.37ms；每SQL/COMMIT注入2ms等待时834.95ms。大响应和并发会放大事务及同账本写锁竞争 |
| 优先优化 | 按流式事件反复查数据库 | principal模式下100帧订阅鉴权402次SELECT，119.17ms；独立预算生成器100帧410次SELECT，150.35ms。注入每SQL 2ms等待时分别1382.26ms、1420.29ms。两组是独立测量，不能直接相加当作所有真实请求的耗时 |
| 优先优化 | 相同正文在整组、逐消息、工具投影、输出及出口重复扫描 | 正常财报文本64,000字符单次工具投影54.29ms；公共模型消息保护114.38ms。250,000字符消息保护537.73ms，1,000,000字符1942.96ms；多轮大上下文会重复付出这部分CPU开销 |
| 次级核查 | 默认原生异步SDK每响应新建、关闭HTTP客户端 | 失去该客户端跨响应复用连接的机会，可能增加建连/TLS等待；未访问真实网络，未给出毫秒结论。不能直接用跨不同事件循环的全局异步客户端替换 |
| 次级核查 | Admin服务凭证冷启动/续期在锁内同步请求 | token缓存命中时开销小；冷启动/过期时锁内请求身份服务，配置请求timeout为5秒，同实例其他调用可能等待该锁。实际网络、失败重试与超时累计未测，5秒不是已证明的整体耗时上限 |

本轮注入检测器没有调用第二个模型或远端分类服务，直接开销来自本地文本处理。不能据此判断整个投研请求的主要耗时比例：真实模型生成、现有规划/搜索流程及业务HTTP均未计时。

## 代码依据

1. **完整缓冲**：[公共原生模型流](../../../backend/app/llm/input_guard.py:180)在`async for`结束后才`yield buffer.checked()`；[直接模型流](../../../backend/app/llm/deepseek.py:588)也先收集全部事件再发布。[持久化订阅](../../../backend/app/conversations/execution.py:503)要等producer封口后重建、检查正文，进度仍可展示。多层安全出口会继续处理完整结果，因此首字时间与模型原始TTFT应分别记录。
2. **字节信用**：[ResponseMeter.consume](../../../backend/app/security/run_budget_http.py:164)用完信用后调用[reserve_response_credit](../../../backend/app/security/run_budget.py:381)，默认64KiB。每次先`charge(binding)`读取预算与run，再无效UPDATE取锁、读取预算与run、更新信用并提交。无压缩响应也分别计wire和decoded。1MiB两维共32次信用预留，解释了上述192条SQL和32次提交。
3. **同步阻塞**：[异步HTTP读取](../../../backend/app/security/http_response.py:110)使用同一个同步`feed`和`meter.consume`；[异步工具网关](../../../backend/app/tools/execution_gateway.py:193)直接调用同步`refresh_tool_context`和`charge`。这些数据库调用以及CPU扫描没有自动让出事件循环；慢连接、锁等待或长正文可能拖慢同一事件循环的其他任务。代码未为这些安全查询设置独立的statement/lock等待上限，HTTP的异步timeout也不能在同步调用执行中将其抢占。
4. **每帧查询**：[authorized_event_stream](../../../backend/app/security/stream_authorization.py:171)推进前、发送前各检查一次；每次[StreamAuthorization._check](../../../backend/app/security/stream_authorization.py:90)在principal模式读取权限目录，再查会话/运行。100帧需200次检查，结束检查再加一次，每次2条SELECT。mock模式没有目录查询，但仍查资源归属。[budgeted_call](../../../backend/app/security/run_budget.py:245)每次推进前后都`charge()`；无增量的charge仍调用`load_budget_binding`并读取预算及run。外部SSE守卫和内部生产器的实际组合依协议而异。
5. **重复扫描**：[消息保护](../../../backend/app/llm/input_guard.py:50)先扫描整组内容，再在逐消息分支重扫各字段，ToolMessage又经过工具投影及模型副本检查；[model_input_view](../../../backend/app/security/model_input.py:9)每次构造策略，默认flag也执行注入检测，返回数据时丢弃风险元数据。[工具结果](../../../backend/app/tools/execution_gateway.py:528)对workflow、receipt、trace再投影，[最终输出](../../../backend/app/security/model_output.py:40)也有逐字段及整体检查。重复检查有出口/策略语义，不能仅凭正文相同直接全部跳过。
6. **连接与冷启动**：[RequestScopedAsyncClient](../../../backend/app/security/sdk_http.py:381)每次send创建拥有自身生命周期的客户端；[DeepSeek装配](../../../backend/app/llm/deepseek.py:77)在未注入异步客户端时选用该实现。[AdminServiceAuth](../../../backend/app/security/admin_service_auth.py:76)在普通线程锁内获取缓存或同步请求token，命中缓存和冷启动应分开统计。

## 测量方式与边界

原始数据见[JSON](2026-11-10-security-latency-results.json)，复现脚本见[局部计时脚本](../probes/security_latency.py)。在仓库根目录执行：

```sh
PYTHONPATH=backend .venv/bin/python \
  docs/security/probes/security_latency.py /tmp/bsagent-security-latency-results.json
```

仅使用合成中英文财报、假模型和临时SQLite；audit hook拒绝网络连接、DNS与sendto。脚本只在自己的进程内替换配置和权限提供方，临时数据库结束后清理，不改`.env`或业务数据。文本与chunk合并各计时3次，表内CPU值为中位数；数据库场景各执行一次并计SQL/提交次数。每个CPU样本有3秒中止保护，防止诊断本身长时间卡住。本轮没有样本触发该上限。

2ms注入是通过SQLAlchemy事件中的`time.sleep`制造数据库等待，用于验证放大关系；操作系统调度使实际等待可能超过2ms，结果不能当作真实数据库RTT预测。未测生产p50/p95、并发压力、真实PostgreSQL、多进程、TLS或真实模型端到端时间。

局部排除项：MessageBuffer纯合并100/1000/4000个短chunk的中位耗时分别1.14/11.14/47.05ms，本样本未证明它是主要热点；不包含最终正文检查，也不代表所有工具调用chunk结构。受管OTLP使用BatchSpanProcessor，5秒force_flush位于清理/关闭路径，没有发现每个token都同步等待远端审计写入。历史重放从头重建存在有界读放大，但当前订阅缓存同一个封口快照，不是每帧都从头扫描。

## 建议的收敛顺序

1. 合并预算信用的校验、锁定和预留事务；评估两维合并/适度批量预留，避免每64KiB重复读run及提交。保留原子限额、身份绑定、失败关闭和不退款语义；优先处理异步链路中的同步数据库阻塞。
2. 合并流式授权/资源读取与预算绑定查询，减少每次检查的往返。保留实际派发/发送前的当前授权；引入缓存或降低检查频率前必须明确撤权时效，不能直接删除后置检查。
3. 将整组大小/结构预算验证与正文转换分开，使同一内容在一个受信调用边界只处理必要次数。安全副本复用必须绑定策略版本、出口、来源和授权上下文，不能信任工具自报“已处理”。
4. 保留用户确认的检查前不展示正文；用安全进度改善等待感，并分别观测模型原始首token、检查耗时、正文首次可见时间。HTTP连接复用另按事件循环生命周期处理。

上述为待实施优化建议，不是本轮已修复结论，也不引入新安全服务或业务能力。
