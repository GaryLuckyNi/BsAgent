# 长连接持续授权验收

实施计划始于2026-11-04，收尾验收日期2026-11-06。范围为 BsAgent 本地实现、临时数据库及合成数据；没有部署、迁移或修改外部 RAG。复用现有 OIDC/JWT 和应用权限目录。

## 实现边界

原 SSE 入口只在建立连接时检查权限，已经载入的旧事件可在撤权后继续输出。JWT主体没有保存验签后的到期时间；首次 POST 与后台委派共用主体时也可能丢失请求期限。现在通过独立 `security/stream_authorization.py` 在公共流层执行持续读取授权：

| 接入点 | 当前要求 |
| --- | --- |
| GET run events | 初始及当前 `agent:read`，原租户/用户/session/run归属，session未删除，JWT未到期 |
| POST protocol首次/恢复/幂等重放 | `agent:invoke`及对应资源；绑定初始化/恢复任务之前的原请求主体，后台委派不替代请求期限 |
| 普通 messages/stream | `agent:invoke`及原主体的会话；新会话可由已授权请求创建，一旦存在即绑定其内部key，不能转入其它会话 |
| AG-UI新任务 | 与普通SSE相同，采用官方事件编码 |
| AG-UI审批恢复 | 决策前及输出期间检查 `agent:invoke`、`approval:decide`；只绑定已核验审批记录的原run、requester、租户和session，保留既有approver语义 |
| 推进及发布 | 推进源前和发布每帧前检查；当前目录与资源查询结束后再次检查本地到期时间，慢查询不能跨过期限后放行 |
| 故障与拒绝 | 固定 denied/expired/unavailable 错误，原异常链不保留；不附恢复id，不生成持久化run_status，不打印源正文或连接异常 |

`Principal.expires_at` 为验签后 exp 派生的可选数值时间戳，目录认证映射保留；非数值、非有限数值和转换溢出按固定无效令牌拒绝。JWT模式缺失可信期限时不放行；mock保留开发测试用途，不作为生产认证证明。后台任务继续依照持久化权限委派执行，订阅令牌到期不等于工具未执行。

持续授权本身不写运行、消息或事件。durable订阅关闭不停止独立生产者；普通SSE/AG-UI既有同步生成器关闭时仍执行原断线收尾，尚未完成的turn可能结束，已经完成的状态和已经发生的副作用不能据订阅错误伪装成未执行。源被关闭，订阅检查不长期占用数据库连接。

## 前端与SDK契约

GET重放解析器识别固定授权错误并停止自动重连，保持最后安全正文、已确认游标及已知真实终态。HTTP401/403/404和明确的权限目录503同样分类；普通网络错误保留恢复行为。

首次POST使用真实 `FetchStreamTransport/useStream`。独立复核发现SDK重建Error会丢弃code，早期实现因此仍自动GET恢复。修复后固定SSE错误携带 SDK 保留的 `name=RunSubscriptionAuthorizationError`，HTTP拒绝由受信fetch适配分类；hook在onError、onFinish和恢复入口统一保留授权停止标志。换用新身份凭证、显式新请求/恢复会重新授权，不将拒绝永久带入新请求。此标志不替代服务端授权。

## 验证方法

- 服务端关联 **106个文件、1984 passed、6 skipped**：[日志](2026-11-06-stream-authorization-tests.log)、[JUnit](2026-11-06-stream-authorization-tests.xml)、[清单](2026-11-06-stream-authorization-test-files.txt)。5项跳过缺少真实PostgreSQL迁移地址，1项真实DeepSeek质量评测未启用，不计通过；不将该关联集合称为整个仓库全量测试。
- 持续授权专项 **25 passed**：[日志](2026-11-06-stream-authorization-unit-tests.log)、[JUnit](2026-11-06-stream-authorization-unit-tests.xml)。JWT/目录相关用例也包含在上述关联集合中；专项和关联统计重叠，不相加。
- 前端 **24个文件、245 passed**：[日志](2026-11-06-stream-authorization-frontend.log)、[JUnit](2026-11-06-stream-authorization-frontend.xml)。构建通过，见 [构建日志](2026-11-06-stream-authorization-build.log)；既有第三方注释、循环chunk/体积和npm配置警告仍保留。
- 独立只读复核发现并修复首次SDK流自动恢复旁路；最终后端25项、前端关联89项通过，所审范围未发现剩余P1/P2。新增慢查询跨期限用例也独立复验通过；不代表全部安全TODO完成。
- 新模块、JWT/Principal/目录、runs和专项测试Ruff通过。sessions的32条B008与HEAD相同；AG-UI保留6条已有B008并修正import排序，没有新增检查项。`git diff --check`及本批文档本地链接核对通过。

主要先失败再修复的场景：同一已载入批次中撤销成员/权限、用户停用、会话删除；源推进期间撤权；JWT期限丢失及非有限日期；空必需权限配置；首次SDK流及HTTP认证拒绝后自动重连；慢目录查询跨过令牌期限后先泄漏正文再拒绝。

使用真实目录和SQLite仓储、FastAPI TestClient、真实RSA签名JWT、真实React/FetchStreamTransport/SDK及内存流。AG-UI决策调用在授权测试中用计数替身隔离外部副作用；拒绝发生在决策前时计数为0，决策已接受后撤权则不再发布正文。前端在JSDOM运行，不将内存流称作真实浏览器网络验收。

正常对照包含 invoke-only POST、read-only GET、授权approver的原资源恢复、新凭证重新订阅、普通断线恢复、后台真实终态及旧payload缺少expires_at的兼容读取。两个旧测试替身已修正：审批资源在请求前提交；仅捕获队列payload的_response替身接受新增必需安全上下文参数，未放松服务端检查。

复跑服务端关联集合：

```sh
xargs .venv/bin/python -m pytest -q --tb=short < docs/security/reports/2026-11-06-stream-authorization-test-files.txt
```

前端在 `frontend` 执行 `npm test` 和 `npm run build`。

独立复核的重点复跑集合：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend .venv/bin/python -m pytest -p no:cacheprovider backend/app/tests/unit/test_stream_authorization.py -q
cd frontend
npm test -- src/hooks/stream-authorization-sdk.test.tsx src/hooks/use-research-session.test.tsx src/services/research-runs.test.ts
```

11月4日的分阶段日志保留作过程证据，最终统计以11月6日文件为准。

## 保留的TODO

1. 检查基于已提交的目录/资源状态，发生在推进/发布边界；不是与网络写原子的即时撤销。已发出或排队的字节不能撤回；源的在途调用仍需等待返回后检查，不承诺强行终止远端请求。
2. IdP/JTI主动撤销、JWKS撤钥和应用目录同步需要可信信号；本批只证明应用目录停用/撤权及已验证exp，不宣称已接通企业IdP实时登出通知。
3. 每帧新授权查询的生产数据库成本、连接数、慢客户端、全任务/网络预算、独立安全决策审计及故障保留期继续待验收。
4. 未定义业务分类、动态业务视图例外、其它隐藏出口、RAG服务身份/文档ACL、历史原文清理和真实模型多轮注入不由本批推定完成。
5. 新版本读取旧任务payload已验证；HTTP/worker/前端版本一致性与实际OIDC配置、生产部署仍需单独验收，不承诺旧worker识别新principal字段。

总目标继续按 [安全TODO](../TODO.md) 验收，不以本批通过勾选整体身份、安全审计或注入防护总项。
