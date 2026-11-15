# 持久化协议重放与未封口历史内容验收

日期：2026-11-04。范围为 BsAgent 本地维护修复，延续用户确认的“正文完整接收并检查后展示，进度仍可继续”。使用合成凭证和临时 SQLite/真实仓储/API；没有执行真实模型/RAG/OCR 调用，没有部署、迁移或提交。

## 修复内容

原 `subscribe_events` 直接返回持久化 JSON，旧凭证可被拆为单字符、跨200条分页或从中间 cursor 重放，绕过单帧检查；活跃状态接口与历史列表也可能提前显示未封口部分正文。

新增独立 `security/protocol_replay.py`，公共订阅及历史适配复用共享凭证底线：

| 边界 | 当前行为 |
| --- | --- |
| 持久化 messages | 按 run_started 阶段拼接全部正文，完整检查后按原片段长度分配安全文本；不同消息 ID 不分割实际客户端会拼接的正文，保留原消息 ID/角色/SSE序号 |
| 中间 after/Last-Event-ID | 重建检查上下文仍从前文开始，发送仍严格遵守 cursor，不能从凭证中间开始检查 |
| 未退出的生产者 | 正文暂缓；正文后的 progress/answer_sources 可无 SSE id 预览，恢复 cursor 留在正文之前；封口后正式按原序发送 |
| 前端恢复解析器 | 只接受无序号进度/来源预览且不推进 cursor；无序号正文、final、run_status 不视为成功或正文；正式事件保持去重 |
| 活跃状态与历史列表 | 正文、消息状态、运行状态和 producer_done 从同一 SQL 快照读取，不复用 ORM 旧部分正文；未封口 assistant 返回空正文 |
| 搜索和会话预览 | SQL 匹配排除未封口 assistant；title/preview 完整取出，经共享检查后再截至160/320字符，避免截出低于已知凭证识别门槛的短前缀 |
| 旧来源/进度/错误/final | 生成公共安全副本；来源 URL 使用现有 public_sources，不把带凭证 URL 改成星号后保留可点击目标 |
| 格式、检查器故障或预算超限 | 扣留正文，不伪造完成/失败，不重跑图/工具，不更新原事件、原消息或真实运行终态；数据库故障不降级返回原文 |

读取预检最多20,000事件、2,000,000正文字符，按200条分页；单字符串仍受 public_output 的检查上限约束。该限制是预检预算，不代表已实现整体任务、输出事件数或数据库/网络响应字节预算。

正文会等到生产者退出（含中断、失败或取消）后再重放，较之前模型缓冲还可能增加完成收尾时间；任务进度仍可预览。取消/失败后的安全部分正文继续保存在历史和后端重放中；前端停止期间原有抑制正文显示的行为保留。

## 验证证据

- 服务端关联 **105个文件、1955 passed、6 skipped**：[日志](2026-11-04-protocol-replay-tests.log)、[JUnit](2026-11-04-protocol-replay-tests.xml)、[复跑清单](2026-11-04-protocol-replay-test-files.txt)。5项跳过需要真实 PostgreSQL 迁移地址，1项真实 DeepSeek 压缩质量评测未启用，不计通过。
- 新专项33项与既有 durable API 8项，合计 **41 passed**：[日志](2026-11-04-protocol-replay-unit-tests.log)、[JUnit](2026-11-04-protocol-replay-unit-tests.xml)。包含单字符 JSON/Bearer/Cookie/PEM/URL 凭证、分页与中间 cursor、不同消息 ID、阶段重置、进度预览后封口、取消与下一轮、检查故障/低预算、缺失执行句柄、关闭连接释放、真实 HTTP Last-Event-ID、活跃快照竞争及历史预览；另通过真实服务分别将未闭合 JSON 部分正文结束为失败/取消/中断，检查安全重放与原状态/原文保留。
- 前端 **23文件、229 passed**：[日志](2026-11-04-protocol-replay-frontend.log)。构建通过，见 [日志](2026-11-04-protocol-replay-build.log)；既有循环 chunk、体积、第三方注释及 npm 配置警告仍保留。
- 独立只读复核发现并修复活跃状态的双 Session 封口竞争，**38项后端、103项会话API、4项前端订阅**通过；随后追加的3项终态部分正文也独立通过，所审范围未发现剩余 P1/P2。上述统计和关联集合重叠，不相加。
- 本批新模块、专项、execution、runs、repository Ruff 通过。sessions 的32条 B008 与 HEAD 已有项相同，不宣称全工程静态检查通过。`git diff --check` 与本批文档本地链接核对通过。

先失败后修复的主要场景：跨帧/分页/中间 cursor 旧凭证；active 原始正文；不同消息 ID 切开检查；前端忽略无序号预览；无执行句柄时反复读取；双 Session 组合旧部分正文与新封口标志；历史列表提前显示部分正文。预览截断测试明确保留对照：先截断会留下未达到已知 key 长度门槛的短前缀，完整检查后截断则不出现此前缀。

封口竞争复现使用两个真实 Session：读取旧 `sk-` 短前缀后，另一 Session 补齐完整 key 并提交 interrupted/producer_done。修复后返回新一致快照的 `interrupted` 与 `***`，不返回旧部分 key。普通公开事实、财报数字、片段、ID、序号、完成/取消状态和原始数据库行均有对照。

复跑：

```sh
xargs .venv/bin/python -m pytest -q --tb=short < docs/security/reports/2026-11-04-protocol-replay-test-files.txt
```

独立复核的最终专项命令：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend .venv/bin/python -m pytest -p no:cacheprovider backend/app/tests/unit/test_protocol_replay_security.py backend/app/tests/integration/test_durable_run_api.py -q
```

前端在 `frontend` 执行 `npm test`、`npm run build`。本批验证的是后端真实仓储/HTTP 和前端 SSE 解析，未用前一批浏览器截图代替本批协议验证。

## 后续 TODO

1. 当前重建针对现有持久化协议的 messages 正文与 run_started 阶段；未知自定义协议、其它隐藏客户端/订阅出口、任意业务改写或编码仍须按真实消费方式验收。
2. 订阅建立后的持续权限撤销、会话删除/用户停用与在途读取、IdP 撤销同步仍待专项；原有入口 tenant/user/session 权限检查继续执行，本批未将其扩展为持续授权。
3. 原数据库/对象/checkpoint 和旧事件原文未批量清理，安全展示副本不能证明历史秘密已经删除。
4. 预检限制不能替代数据库响应字节、整个事件输出、整体执行时间、并发、重试、停止后的在途 HTTP 与副作用预算；继续按 SEC-06/09/10 验收。
5. 业务分类、真实模型多轮注入、图片像素接收方、RAG 服务身份/文档 ACL 与真实部署不由本批推定完成。

整体安全目标保持进行中，对应总项继续见 [安全 TODO](../TODO.md)。
