# BsAgent 生产化与图片分析实施路线图

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 BsAgent 建设为具备安全图片上传、OCR + Vision 混合分析、Agent 全链路消费和生产部署能力的可上线项目。

**Architecture:** 原始图片由 Asset Service 校验后存入私有 S3/MinIO，异步 Worker 通过 PaddleOCR 与多模态 API 生成 `ImageAnalysisArtifact`。LangGraph 只保存 Asset/Artifact 引用与轻量摘要，生产入口通过 JWT/JWKS、租户策略与 Tool Gateway 实施确定性治理。

**Tech Stack:** Python 3.11、FastAPI、LangGraph、PostgreSQL、SQLAlchemy、Alembic、Redis、Celery、S3/MinIO、Pillow、PaddleOCR HTTP Service、OpenAI-compatible Vision API、Vite、pytest、Playwright、Docker Compose、OpenTelemetry

---

## Phase 0：审批恢复与 Mock RBAC 收口

- [ ] 审核当前工作区已实现的审批指纹、Approval Gate、Checkpoint Resume、Approval Repository 和 Mock RBAC。
- [ ] 补齐参数哈希不匹配、跨租户、重复决策和拒绝恢复测试。
- [ ] 验证 `20260713_0004_approval_records` 迁移链。
- [ ] 更新 Checkpoint、Tool Gateway 和 Mock RBAC 中文文档。
- [ ] 全量测试、Ruff 与 diff check 通过后独立提交。

权威计划：`docs/superpowers/plans/2026-07-13-approval-resume-rbac.md`

## Phase 1：生产身份与图片 Asset 基础

- [ ] 统一 `Principal` FastAPI Dependency，Mock 仅允许非生产环境。
- [ ] JWT/JWKS 验证签名、issuer、audience、`exp`、`sub` 和 `tenant_id`。
- [ ] 建立 Asset Schema、Repository、Local/S3 Store SPI 与 Alembic 迁移。
- [ ] 建立图片格式、Magic Bytes、大小、像素、动图和 EXIF 清理链路。
- [ ] 实现鉴权 Asset CRUD API、租户归属、过期、去重和分析任务端口。

详细计划：`docs/superpowers/plans/2026-07-24-image-asset-foundation.md`

## Phase 2：OCR + Vision 异步分析

- [ ] 实现 `OcrProvider` 和 `VisionProvider` SPI。
- [ ] 实现 PaddleOCR HTTP Adapter 与 OpenAI-compatible Vision Adapter。
- [ ] 实现 Celery 幂等任务、有界重试、熔断和并发限制。
- [ ] 实现 Artifact 合并、冲突警告、`ready|partial|failed` 状态机。
- [ ] 实现分析缓存键与租户级 `allow_external_vision` 策略。

## Phase 3：LangGraph 多模态链路

- [ ] 将 `attachment_analyzer` 接入 Graph 入口。
- [ ] State 只增加 `attachment_refs` 和 `image_analyses`。
- [ ] Intent Router、Planner、Skill、Synthesizer 和 Verifier 消费图片摘要。
- [ ] 实现 `asset_not_ready`、`asset_analysis_failed` 与 partial 回答契约。
- [ ] 验证 Checkpoint 和 Trace 不含图片、Base64 或完整 OCR 文本。

## Phase 4：前端附件体验

- [ ] 实现图片选择、预览、移除、上传和识别状态。
- [ ] 实现发送前 `ready|partial` 门禁。
- [ ] 实现消息缩略图、SSE 阶段事件和可操作错误。
- [ ] 通过桌面、平板和移动端 Playwright 验收。

## Phase 5：生产部署与真实验收

- [ ] 接入 PostgreSQL Checkpointer、Redis 限流、MinIO/S3、Celery Worker 和 PaddleOCR Service。
- [ ] 实现 API、Worker、Frontend Dockerfile 与本地生产 Compose。
- [ ] 实现 Alembic 迁移任务、liveness/readiness 和优雅停机。
- [ ] 实现 JSON 日志、OpenTelemetry 接口与脱敏 Trace。
- [ ] 使用真实财报截图、表格和 K 线图完成端到端验收。
- [ ] 完成中文部署、运维、备份、数据过期和故障排查文档。

## 阶段门禁

每个 Phase 必须满足：

1. 行为变更先观察失败测试 RED，再实现到 GREEN。
2. 定向测试、全量后端测试、Ruff 和 `git diff --check` 通过。
3. 修改前端时，类型检查与生产构建通过。
4. 实现、迁移、配置和中文文档一致。
5. 阶段改动独立提交，不混入无关重构。

## 最终完成审计

逐项对照 `docs/superpowers/specs/2026-07-24-production-image-analysis-design.md` 第 17 节，使用当前代码、命令输出、容器状态、迁移结果、真实图片运行记录和渲染截图证明全部要求成立。
