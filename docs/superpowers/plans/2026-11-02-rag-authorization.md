# 外部 RAG HTTP 工具的调用方安全加固

日期：2026-11-02。用户已确认实际接入为 BsAgent 内置 rag_search HTTP 工具。

本批范围：只改 BsAgent 调用方，保持 POST /api/rag/retrieve 和已有 payload/headers 契约。独立外部服务的认证、可信身份传播和文档 ACL 另按它实际支持的协议设计，不能由调用方推断已经完成。

## 执行项

- [x] 核对实际注册：tools/rag_search.py 是 HTTP 适配器，由 DefaultRegistry 注册为 rag provider；不是 MCP 检索工具。
- [x] 先补失败用例：身份不匹配仍能外发、模型可选择 tenant/user、真实权限模式缺上下文直接调用。
- [x] 隐藏模型身份参数，由受信任务上下文提供；参数不一致拒绝，principal 模式重新核对任务身份、原始权限上限和当前 rag:retrieve。
- [x] 保持实际外部 HTTP 请求契约，用正常调用验证兼容性。
- [x] 修复外发前最后一次授权失败的回执分类，验证 scopes 撤销、成员撤销、目录故障均不外发且回执为 denied/not_started。
- [x] 限制上述分类只信任内置 RagSearchTool 及 rag execution binding，不允许任意工具用同名错误宣称未执行。
- [x] 完成相关回归、独立复核、证据与 TODO 更新。

## 范围纠正与保留事项

最初读取了相邻 rag 目录，并在 /private/tmp/bsagent-rag-security 准备过服务端安全探索稿；用户澄清外部服务边界后，没有向该仓库应用补丁、创建分支、安装依赖或修改数据。该目录是否与实际部署同源未确认，不能作为部署事实。

主工程临时拟定的私有签名协议、密钥配置和 OAuth 公共提取均已撤回，不要求外部 RAG 支持自定义签名头。相邻工程保持原状态。临时探索稿不是发布制品，不应自动继续实施。

后续需取得外部 HTTP 服务的认证文档/OpenAPI、受信调用方规则及文档可见性契约；再决定服务 token/API Key、身份委派或固定部署授权。无证据时保留 TODO，不从 filters、任意 Header 或返回 metadata 推断文档权限。无需另建 RAG 登录页。

证据：619 项相关回归通过；独立专项 10 项通过。见 docs/security/reports/2026-11-02-rag-caller-implementation.md。
