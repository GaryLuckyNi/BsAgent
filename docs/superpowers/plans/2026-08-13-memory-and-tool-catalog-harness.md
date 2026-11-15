# 记忆与工具目录 Harness 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 LangGraph 官方 Store 补齐基础长期经验记忆，并建立 MCP 工具目录的启动发现和冻结快照生命周期。

**Architecture:** 长期经验通过 `BaseStore` 的 namespace/key 持久化，候选与审批继续留在业务数据库。MCP 使用官方 Adapter 启动发现，Harness 保存冻结 Registry，模型渐进披露 ToolSpec。

**Tech Stack:** LangGraph、PostgresStore、InMemoryStore、LangChain MCP Adapter、FastAPI、pytest。

---

### Task 1：LangGraph Experience Store 适配

- [x] 为 namespace 隔离、稳定 key 覆盖和有界召回编写失败测试。
- [x] 实现 `LangGraphExperienceStore`，只依赖 `BaseStore`。
- [x] 增加生产 `PostgresStore` 与测试 `InMemoryStore` 工厂。
- [x] 保留现有 Candidate 和 Approval 审计链路。

### Task 2：基础结构化偏好

- [x] 为语言、回答详细度和关注领域编写候选解析测试。
- [x] 为 Candidate 增加稳定 `memory_key`，同 key 使用 Store 覆盖。
- [x] 将 Memory Retriever 改为查询有界相关记忆。
- [x] 更新 API 执行快照与 Graph 注入。

### Task 3：工具目录生命周期

- [x] 为启动发现一次、冻结快照和失败 readiness 编写测试。
- [x] 实现进程级 `ToolCatalogRuntime`，包装官方 MCP 发现结果。
- [x] 应用 lifespan 预热目录，Run 只读取冻结快照。
- [x] 评估渐进披露：当前单 MCP、少量工具无需增加筛选层，达到规模阈值后再启用。

### Task 4：Harness 与验证

- [x] 为记忆读取失败增加安全 Trace summary，写入失败记录安全结构化日志。
- [x] 更新中文 Memory、Tool 和生产运行文档。
- [ ] 运行定向测试、后端全量、MCP 协议、Ruff 和 Compose 检查。
