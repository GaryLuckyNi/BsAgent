# 首轮本地红队验证报告

日期：2026-11-02。运行标识：`2026-11-02-local-01`。源码基线：`8d1fed4e`，工作区已有本轮安全文档；本轮未修改应用实现。

## 1. 结论与适用范围

本轮完成两个定向探针和 253 项既有测试。两个探针分别复现 Admin 应用层缺少调用者准入，以及 RAG 本地调用身份约束与出站授权上下文缺口。**未证明生产公网暴露或真实 RAG 跨租户泄露，也未完成 32 类案例的端到端验收。**

后续结合用户确认的身份建设现状，已补充 [身份基础分析](../auth-foundation-analysis.md)：主应用已有 OIDC/JWT 能力但本地默认 mock，Admin 与 RAG 尚无完整调用者身份链；RAG 存在部署固定分区。以下两项应理解为尚未建设或接通的身份边界，而非完整登录体系被绕过，原始本地事实与测试层级不变。

| 验证项 | 结果 | 证据 |
| --- | --- | --- |
| Backend 安全相关单元基线 | 227 项通过，退出码 0，1 条警告 | [日志](2026-11-02-backend-baseline.log)、[JUnit](2026-11-02-backend-baseline.xml) |
| Admin 数据源与 Skill API 基线 | 26 项通过，退出码 0 | [日志](2026-11-02-admin-baseline.log)、[JUnit](2026-11-02-admin-baseline.xml) |
| RT-15 本地网关与 HTTP 适配器边界 | FAIL，限白盒及 mock 上游层 | [请求捕获与结果](2026-11-02-rag-probe.json) |
| RT-25 Admin 应用准入 | FAIL，限进程内 HTTP 应用层 | [HTTP 状态与状态变化](2026-11-02-admin-probe.json) |
| 真实上游 ACL / 生产 Admin 准入 | BLOCKED，缺少实际部署与授权环境证据 | 不根据本地结果推断生产可利用性 |

既有基线通过说明所选用例没有回归；它们并未断言本轮两个探针要求的拒绝行为，所以不能用通过数覆盖新发现。

## 2. 环境、身份与执行限制

- Python 使用项目 `.venv/bin/python`；后端与 Admin 的 `app` 包在不同进程导入，避免命名冲突。
- 每个进程进入独立临时目录；Admin 使用专用临时 SQLite，结束后清理。后端探针使用 `httpx.MockTransport`；没有连接真实 RAG、MCP、模型、数据库或观测服务。
- 清除进程内继承的 `BS_AGENT_*` 设置后显式使用 test 配置；关闭 Langfuse/OTEL，后端数据库为内存 SQLite，避免继承真实服务配置。
- Python audit hook 阻止探针进程内的 DNS 查询、IPv4/IPv6 connect 与 sendto；四次运行均记录 0 次阻断事件。该 hook 是本次测试约束，不是产品网络沙箱或通用安全能力。
- 本轮外部模型调用为 0，外部费用为 0；最多两个测试进程并行。没有运行真实提示注入评估、压力测试、生产配置修改或真实权限变更。
- 合成主体：`rt-tenant-a/rt-user-a1` 与 `rt-tenant-b/rt-user-b1`；均仅有 `rag:retrieve` 和本地 Plan 中的 `rag_search`。使用合成 B 私有标记作为 mock 结果，不放进 A 的攻击请求。
- Admin 对比匿名请求与带无效合成 Bearer/Header 的请求；没有伪称持有真实普通用户 token。合法调用正例是临时数据库中的配置及读取，证明接口可用；尚无服务身份鉴别的正例。

## 3. 发现 A：Admin 应用层不区分匿名调用者与授权控制面调用者

**事实等级：可复现的应用层行为；生产可达性待补证。优先级：P0 验证与修复设计。**

来源：[内部连接接口](../../../admin/backend/app/api/internal_market_data_providers.py)、[数据源写接口](../../../admin/backend/app/api/market_data_providers.py)、[应用装配](../../../admin/backend/app/main.py)。

隔离复现步骤：

1. 用 `create_app()` 启动进程内 TestClient，数据表在临时 SQLite 中创建。
2. 不提供认证信息，创建并激活合成数据源：返回 `201`。
3. 普通详情返回 `200` 且连接秘密被遮蔽，说明现有展示脱敏有效。
4. 匿名访问内部连接接口：返回 `200`，包含完整合成 MCP URL。
5. 携带无效 Bearer 和任意用户 Header 再次访问：仍返回 `200` 和完整合成 URL。
6. 匿名更新数据源 URL：返回 `200`，随后读取确认更新已持久化。

因此，一旦非授权主体能够直接到达此应用，它不仅能读取连接，还能修改 Agent 后续使用的数据源配置。测试未访问真实连接，使用的秘密均是固定合成标记。

[生产部署说明](../../production-deployment.md)已要求内部接口只在服务网络开放；实际网关、网络和服务身份配置未提供，不能将此结果写成“互联网可匿名窃取生产凭证”。仅隔离公网流量也不足以回答同服务网络内的非授权主体能否访问，需要明确准入主体。

建议在控制面公共入口统一接入用户/服务身份及操作权限，区分浏览器配置读写与服务端连接读取；同时验证部署网关和网络约束。保持脱敏详情的正常路径，不把完整连接下发给模型或普通浏览器。

关联：TH-03；SEC-11、SEC-05、SEC-02；RT-25。

## 4. 发现 B：RAG 网关与适配器没有建立可验证的租户身份传递契约

**事实等级：白盒网关与 mock HTTP 行为可复现；真实上游隔离待补证。优先级：P0 设计。**

来源：[RAG 适配器](../../../backend/app/tools/rag_search.py)、[执行网关](../../../backend/app/tools/execution_gateway.py)。

通过真实 `ToolExecutionGateway` 和注册的 `rag_search` 执行三组请求；上游 mock 故意持续返回带 B 租户 metadata 的合成文档：

| 执行上下文 | 参数租户 | Gateway 结果 | 收到 B 标记 |
| --- | --- | --- | --- |
| B / B1 | B | success | 是，授权对照 |
| A / A1 | A | success | 是，mock 未隔离结果被接受 |
| A / A1 | B | success | 是，参数与上下文冲突未被此边界拒绝 |

三次出站 HTTP payload 完全相同，所捕获 Header 无主体授权信息。payload 包含 query、filters、retrieval 和 responseMode，没有 tenant/user；`RagSearchInput` 中有身份字段并不代表上游接收到了可信身份。

本实验说明本地适配层没有约束上下文与参数租户一致，也没有在当前请求上体现用户或租户授权。**mock 是故意不执行 ACL 的模拟提供方，不能据它返回 B 文档就断言真实 RAG 已被越权。** 直接构造 ToolCallRequest 属于白盒夹具，不证明普通用户可控制同样字段。

还需确认真实部署是否按租户独立提供 RAG 服务、如何绑定服务身份及文档 ACL，以及上游是否有其他可信授权来源。即便租户级服务隔离成立，也要确认同租户不同用户的文档权限。当前统一 `rag_base_url` 与所测适配器不足以自行证明这些隔离条件。

建议先定义上游支持的授权协议，再由公共适配层从可信上下文注入或绑定身份并限制用户 filters，执行网关拒绝相冲突的服务端身份字段。不能仅增加一个可伪造的 tenant Header，或把不可信返回 metadata 的简单过滤当作完整授权。

关联：TH-02、TH-06；SEC-03、SEC-04、SEC-05、SEC-02；RT-15。

## 5. 32 类案例状态

以下状态针对本轮测试层和覆盖范围；单元测试通过不自动升级为整个案例通过。

| 案例 | 状态 | 实际覆盖或缺失条件 |
| --- | --- | --- |
| RT-01～RT-12 | BLOCKED | 未提供获准的真实模型出口与费用预算，未执行模型行为/端到端注入评估；mock 固定输出不能证明抗注入 |
| RT-13 | NOT_RUN | 已有 JWT 和依赖基线通过，未执行该案例完整的真实路由/主体交叉矩阵 |
| RT-14 | NOT_RUN | 未执行完整资源、SSE、审批及 Trace 越权矩阵 |
| RT-15 | FAIL | 适配器层缺口已复现；真实上游 ACL 子项仍 BLOCKED |
| RT-16～RT-24 | NOT_RUN | 白名单、审批指纹、副作用确定性等已有部分基线支持，未执行各案例完整序列 |
| RT-25 | FAIL | 应用层匿名读取及写入已复现；实际部署准入子项仍 BLOCKED |
| RT-26～RT-32 | NOT_RUN | 预算、故障、目录、渲染与审计尚未按案例运行；图片和脱敏仅有既有基线证据 |

主表计数：PASS 0、FAIL 2、BLOCKED 12、NOT_RUN 18、NOT_APPLICABLE 0。状态逐项记录见 [JSONL](2026-11-02-red-team-results.jsonl)。没有计算攻击成功率、误报率或稳定通过率，因为本轮缺少模型样本与配对正常任务评估。

## 6. 复现方式

从仓库根目录运行，选择未使用的输出文件名保留每次运行证据：

```bash
.venv/bin/python docs/security/probes/local_checks.py rag
.venv/bin/python docs/security/probes/local_checks.py admin
.venv/bin/python docs/security/probes/local_checks.py backend-baseline --junit /tmp/bsagent-backend-baseline.xml
.venv/bin/python docs/security/probes/local_checks.py admin-baseline --junit /tmp/bsagent-admin-baseline.xml
```

[探针源码](../probes/local_checks.py)只复现并记录观察，发现安全缺口时仍可正常退出；退出码 0 表示探针执行完毕，不表示安全通过。基线模式的退出码使用 pytest 结果。本轮没有把问题修成预期通过，也没有改变应用的权限模型。

## 7. 后续工作

1. 收敛控制面用户/服务身份和 RAG 授权协议；让公共设施接入安全内核，Agent 节点不直接依赖安全模块。
2. 先为两个已复现边界建立安全拒绝断言和合法路径，再实施修复；修复后重跑同样探针，并验收真实部署准入和上游 ACL。
3. 完成 RT-13、RT-14、RT-16～RT-24 等本地确定性序列，再在明确模型预算后补真实模型注入样本；保留当前 BLOCKED/NOT_RUN 状态，禁止宣称全量通过。
