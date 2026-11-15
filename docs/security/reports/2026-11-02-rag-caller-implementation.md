# 第 4 批：外部 RAG HTTP 工具调用方验收

日期：2026-11-02。用户已确认实际接入为内置 rag_search HTTP 工具。范围只含 BsAgent 调用方，不含外部服务端或独立 RAG 工程。

## 变更与结果

- 模型输入 schema 隐藏 tenant_id/user_id；执行时从宿主上下文填充。显式参数与上下文不一致，在外发前拒绝。
- principal 模式必须关联受信任务，重新检查内部用户、租户、会话/request_id、当前 rag:retrieve 和任务初始上限；禁止无上下文直接 run()。
- 网关校验后、RAG 内部最后检查遇到撤权/成员停用/目录故障时，HTTP 请求数为 0，回执按 denied/not_started 保存。
- 上述回执分类只信任内置具体 RagSearchTool 与 rag execution binding，不能仅凭外部执行器错误码、相同工具名或 provider 推断未执行。
- 保持 POST /api/rag/retrieve 的现有 payload 和追踪头；没有新增下游专用签名头。客户端显式不跟随重定向。

相关回归最终 **619 passed，18.06 秒**，其中新增 RAG 调用方专项 **10 项**；见 [日志](2026-11-02-rag-caller-tests.log)、[JUnit](2026-11-02-rag-caller-tests.xml)。测试使用真实本地权限目录/任务/回执及 MockTransport，不连接实际 RAG、IdP 或模型。

回归覆盖原 RAG HTTP 契约、合法受信调用、参数身份冲突、伪造 context、当前权限撤销、模型 schema、网关回执，以及 JWT/目录/任务/审批/原生工具消息/来源计划/目录更新兼容性。正常请求仍可执行，未改变外部服务必须接受的协议。

新增专项独立运行命令：

```bash
PYTHONPATH=backend .venv/bin/python -m pytest \
  backend/app/tests/unit/test_rag_authorization.py \
  backend/app/tests/unit/test_rag_search_tool.py \
  backend/app/tests/unit/test_tool_execution_certainty.py -q
```

Ruff 和 git diff --check 通过。独立审查发现外发前授权异常原先被通用分支记为 failed/uncertain；先以三种拒绝场景复现失败，再限定可信类型修复，另加其他执行器实际执行后抛同名异常仍保持 uncertain 的负对照。最终独立专项复核 **10 passed**，未发现剩余阻断问题。

## 范围纠正

最初将相邻 /Users/myfolder/前端学习项目/rag 当作候选下游源码，读取后在 /private/tmp/bsagent-rag-security 准备过探索稿。用户明确外部服务边界后，未向该工程应用补丁、创建分支、安装依赖或改动知识文件/索引；实际仓库仍是原分支和原有未跟踪资料。

BsAgent 中临时拟定的独立签名协议、密钥配置及服务 token 公共提取均已撤回。临时探索稿不属于交付，也不能作为外部服务已经具有认证/ACL 的证据。Admin 保持第 3 批已经验收的实现，其认证专项随本次继续回归。

## 待确认事项

当前内置 HTTP 适配器默认未附加专用 RAG 服务凭证；请求体也没有可信的用户/租户身份协议。调用者有 rag:retrieve，只能说明可以使用工具，不能证明每份返回文档可见。

外部 RAG 的服务认证、部署租户、可验证用户委派、文档检索前 ACL 仍需根据实际 OpenAPI/认证文档和部署约定验收。不能使用任意 Header、filters 或返回 metadata 推断权限。相关事项保留在 [安全 TODO](../TODO.md)，详细边界见 [调用方说明](../rag-caller-boundary.md)。历史红队报告保持原状态。
