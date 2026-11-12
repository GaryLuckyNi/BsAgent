# 外部 RAG HTTP 工具：调用方安全边界

日期：2026-11-02。用户已明确实际使用内置 rag_search HTTP 工具。本批只修改 BsAgent，不修改外部服务。

## 实际调用链

```text
已认证 Principal → 当前权限目录 → 任务初始权限上限
    → ToolExecutionGateway → RagSearchTool.run_with_context
    → HTTP POST /api/rag/retrieve → 外部 RAG
```

实现位置为 backend/app/tools/rag_search.py，DefaultRegistry 将它注册为 rag provider，执行绑定为 rag.http.retrieve；不是通过通用 MCP 发现的检索工具。地址来自 BS_AGENT_RAG_BASE_URL，工具定义不接受模型选择目标 URL。

原 HTTP 请求体包括 query、filters、retrieval、responseMode；当前协议没有可信的调用用户/租户字段。原 traceparent 和 x-business-trace-id 只用于追踪，不是认证。默认内置客户端当前也未附加专门的 RAG 服务凭证。这些是待补的外部契约，不用自定义 Header 冒充已经接入的身份协议。

## 本批已修复

- 模型可见输入不再包含 tenant_id/user_id；由宿主在执行时从工具上下文提供。旧显式参数与上下文不一致时，在外发前拒绝。
- principal 模式要求受信任务绑定，不能用直接 run() 或自造 context 绕过任务检查；调用前重新校验主体、租户、会话/request_id、当前 rag:retrieve 和任务最初权限上限。
- 网关最后检查之后，RAG 工具内部再次检查若发生权限撤销、成员停用或目录故障，不发 HTTP，并将持久化回执记为 denied/not_started。
- 上述“未执行”分类只适用于内置具体 RagSearchTool 类型及 rag binding。任意其他 executor 即使使用相同工具名、provider 或错误类，也不能把可能已执行的调用改写为未开始。
- 保持现有 HTTP 请求体与追踪头，不要求外部服务接受专用签名协议。客户端显式不跟随 HTTP 重定向。

## 不等于下游文档授权

以上证明“哪些任务可以调用工具”和“模型不能更换调用者”，不能证明外部 RAG 返回的每份文档都对当前用户可见。用户 filters 和返回 metadata 均不能建立授权信任；返回后过滤也不能替代检索前 ACL。

继续接入前需要核对实际外部服务的认证文档/OpenAPI、服务凭证用途、可验证的用户/租户上下文，以及文档可见性规则：

| 外部服务能力 | 后续接入原则 |
| --- | --- |
| 已支持 Bearer/JWT 或 API Key | 使用专门服务凭证和限定目标；按已存在协议接入，不复用 Admin 或用户 token 到未知 audience |
| 已支持可信用户委派与 ACL | 按契约传播受信任务身份，检索前强制当前用户授权，普通 filters 只缩权 |
| 只有固定部署和同租户共享库 | 显式确认部署租户、调用者范围和共享文档范围；不声称支持同租户私有文档 |
| 没有上述能力或证据不足 | 保留为未完成，不能只增加 tenant/user 字段后宣布安全；需服务方改造或经授权引入网关 |

## 本轮范围纠正

相邻 /Users/myfolder/前端学习项目/rag 只做过源码读取；尚未确认它是否与实际部署同源。早期服务端探索稿只位于 /private/tmp/bsagent-rag-security，未向该仓库应用、创建分支、安装依赖或改动知识文件/索引。

BsAgent 中临时拟定的专用签名协议与密钥配置已撤回，Admin 保持上一批已经验收的认证实现。临时探索稿不属于交付实现，也不能据此改变外部服务的验收结论。

测试证据见 [调用方验收记录](reports/2026-11-02-rag-caller-implementation.md)；下游认证和 ACL 继续留在 [安全 TODO](TODO.md)。

## 2026-11-03 公开契约补证

用户提供的 8011 当前有服务监听，实际 GET `/openapi.json` 返回 200，标题 Minimal RAG API；公开 securitySchemes 为空，global security 和 `/api/rag/retrieve` 的独立认证要求均未声明，见 [只读摘要](reports/2026-11-03-rag-openapi-contract.json)。这是公开契约证据，不等于已证实隐藏中间件/代理没有鉴权或 ACL。没有匿名执行真实检索、读取知识文档或改动外部工程，仍需确认真实认证/可信委派/文档授权契约。
