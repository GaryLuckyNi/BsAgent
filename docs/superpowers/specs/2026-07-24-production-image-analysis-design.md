# BsAgent 生产化与图片分析设计

## 1. 背景

BsAgent 已经具备 LangGraph 编排、Intent Router、Planner、Skill、Tool Gateway、MCP、RAG、Checkpoint、Trace、审批和基础 RBAC 能力，并已有金融投研场景的业务实现。当前后端自动化测试和前端生产构建可以通过，但仍缺少图片输入契约、多模态 Provider、对象存储、异步图片任务、真实鉴权与生产部署基线。

本设计的目标是在不破坏现有文本 Agent 链路的前提下，建设可复用的多模态底座，首期优化财报截图、表格图片、公告扫描件、K 线图和行情图，同时补齐上线所必需的安全、存储、运维和验收能力。

## 2. 目标

1. 支持用户上传 JPEG、PNG 和 WEBP 图片，并在对话中通过 `asset_id` 引用。
2. 使用 OCR 与 Vision Model 混合分析，统一产出可审计的 `ImageAnalysisArtifact`。
3. 保留 DeepSeek 作为文本 Intent、Planner 和综合推理模型，图片能力通过独立 Provider SPI 接入。
4. 使 Intent Router、Planner、Skill、MCP、RAG 和 Synthesizer 能够消费图片分析摘要。
5. 原始图片、Base64 和超长 OCR 结果不进入 LangGraph State、Checkpoint 和默认 Trace。
6. 建立可持久恢复的异步图片分析任务、完整错误状态和幂等重试机制。
7. 用 PostgreSQL、Redis、S3/MinIO、JWT/JWKS、容器化和生产配置校验补齐上线基线。

## 3. 非目标

- 不在 BsAgent API 进程内加载重型 OCR 模型。
- 不把 DeepSeek 文本 Chat Completion 当作图片模型使用。
- 不向模型暴露对象存储的永久公开 URL。
- 不在首期支持 SVG、GIF、视频、PDF 或音频。
- 不将 OCR 结果直接写入长期 Knowledge 或用户经验存储。
- 不在首期搭建通用的可视化多模态流程编辑器。

## 4. 总体架构

```text
Frontend
  -> Asset API
      -> Image Security Pipeline
      -> S3 / MinIO
      -> PostgreSQL Asset Metadata
      -> Redis / Celery Queue
          -> Image Analysis Worker
              -> PaddleOCR HTTP Service
              -> External Vision API
              -> ImageAnalysisArtifact

Frontend
  -> Session Message API(message + attachment_ids)
      -> Attachment Analyzer Node
      -> Memory Manager
      -> Intent Router
      -> Planner
      -> Skill / Tool / MCP / RAG
      -> Synthesizer / Verifier
      -> SSE Response
```

架构拆分为四个边界：

- Asset Service 只管理文件、归属、存储和生命周期。
- Image Analysis Service 只协调 OCR、Vision 和 Artifact 合并。
- Agent Graph 只消费轻量 Artifact 摘要与引用。
- 业务 Skill 负责金融、法务、客服等领域的解读规则。

## 5. Asset Service

### 5.1 API

```text
POST   /api/assets/images
GET    /api/assets/{asset_id}
DELETE /api/assets/{asset_id}
```

`POST /api/assets/images` 使用 `multipart/form-data`，成功后返回 HTTP 202：

```json
{
  "asset_id": "asset_xxx",
  "media_type": "image/png",
  "size_bytes": 128400,
  "width": 1920,
  "height": 1080,
  "status": "processing"
}
```

Session API 扩展为：

```json
{
  "message": "分析这张财务报表",
  "attachment_ids": ["asset_xxx"]
}
```

用户和租户信息必须来自服务端验证过的 Principal，不信任请求正文自行声明的身份字段。

### 5.2 安全校验

- 仅允许 JPEG、PNG 和 WEBP。
- 同时校验 Content-Type、Magic Bytes 和解码结果。
- 单图最大 10 MB，最大 2000 万像素，单次消息最多 4 张。
- 使用 Pillow 完整解码并重新编码，移除 EXIF 和非必要元数据。
- 拒绝 SVG、动图、截断图片、异常压缩比与解压炸弹。
- 对安全校验和重新编码设置独立耗时与内存上限。
- 上传、查询、删除和消息引用都必须校验 Asset 归属。

### 5.3 存储

原始上传文件不直接持久化。安全流程只保存重新编码后的标准化图片，并生成 SHA-256。生产环境使用 S3 兼容对象存储，本地测试使用内存或临时目录 Adapter。

默认保留期为 7 天，租户可在管理策略中缩短或延长。删除采用软删除标记与异步对象清理，并保留不含原图的审计记录。

## 6. Provider SPI 与混合分析

### 6.1 Provider 契约

```python
class VisionProvider(Protocol):
    async def analyze(self, *, image, instruction, output_schema): ...

class OcrProvider(Protocol):
    async def extract(self, *, image, languages, detect_tables): ...
```

首期 Provider：

- `OpenAICompatibleVisionProvider`：通过 `base_url`、`api_key` 和 `model` 配置云端多模态 API。
- `PaddleOcrHttpProvider`：调用独立 PaddleOCR 服务，不在 Agent API 进程加载 OCR 模型。
- Fake Provider 仅用于自动化测试，生产环境启动时必须拒绝。

DeepSeek 继续负责纯文本 Intent、Planner、工具规划和回答综合，不修改为伪多模态客户端。

### 6.2 租户外发策略

`allow_external_vision` 是服务端租户策略，不由前端或模型声明。

- 为 `true` 时，Worker 可将标准化图片发给配置的 Vision Provider。
- 为 `false` 时，只允许使用租户允许的本地 Provider。如果本地 Vision 不可用，可降级为 OCR-only `partial`，不能绕过策略外发。

### 6.3 幂等与缓存

分析缓存键：

```text
sha256 + analysis_profile + ocr_provider_version + vision_provider_version
```

同一租户内的相同图片可复用分析结果。跨租户默认不复用、不暴露去重命中信息，避免通过哈希推断其他租户数据。

## 7. ImageAnalysisArtifact

统一 Artifact 摘要契约：

```json
{
  "analysis_id": "analysis_xxx",
  "asset_id": "asset_xxx",
  "profile": "financial_image",
  "image_type": "financial_table",
  "summary": "贵州茅台 2025 年营业收入表格",
  "entities": {
    "company_name": "贵州茅台",
    "stock_code": "600519"
  },
  "tables": [],
  "chart_analysis": null,
  "ocr_excerpt": "",
  "warnings": [],
  "confidence": 0.94,
  "artifact_ref": "artifact://image-analysis/..."
}
```

Artifact 必须区分：

- OCR 直接识别的文本与数字。
- Vision 模型对图表、布局和趋势的视觉推断。
- Provider 缺失、结果冲突和低置信度警告。
- 可供人工复核的原始 Artifact 引用。

完整 OCR 坐标、超长表格和 Provider 原始响应不写入 PostgreSQL 摘要字段，而是保存在私有对象存储中。

## 8. 异步状态机

```text
uploading -> processing -> ready
                         -> partial
                         -> failed
ready/partial/failed -> expired/deleted
```

- `ready`：OCR 和 Vision 都成功。
- `partial`：至少一个 Provider 成功，且结果达到最低可用标准。
- `failed`：两个 Provider 都失败或结果不可用。

使用 Redis 作为 Celery Broker，图片 Worker 独立部署。任务具有稳定 `analysis_id` 和幂等锁，仅对超时、限流、5xx 和短暂网络错误执行有界重试，不重试输入校验和合规拒绝。

## 9. Agent Graph 集成

新增 `attachment_analyzer` 节点，顺序为：

```text
attachment_analyzer
  -> memory_manager
  -> intent_router
  -> planner_agent
```

该节点负责：

1. 校验 `attachment_ids` 全部属于当前 Principal。
2. 加载 `ready` 或 `partial` 状态的分析摘要。
3. 对 `processing`、`failed`、`expired` 返回结构化错误，不让后续模型假装看过图片。
4. 把轻量 `AssetRef` 和 `ImageAnalysisSummary` 写入 State。

State 新增：

```text
attachment_refs
image_analyses
```

Intent Router 的分类输入会包含图片类型、摘要、实体和警告。Planner 可基于图片中的公司、证券代码和财务期间调用后续 Skill、MCP 和 RAG。Synthesizer 必须保留事实、推断、置信度与局限性边界。

## 10. 数据模型

### 10.1 `asset_records`

```text
asset_id, tenant_id, user_id, storage_key, sha256,
media_type, size_bytes, width, height, status,
created_at, expires_at, deleted_at
```

### 10.2 `image_analysis_records`

```text
analysis_id, asset_id, profile,
ocr_provider, ocr_version, ocr_status,
vision_provider, vision_version, vision_status,
summary_json, artifact_storage_key,
confidence, warnings_json,
created_at, completed_at
```

PostgreSQL 生产表应使用 JSONB 保存结构化摘要与警告，并为 `tenant_id`、`user_id`、`sha256`、`status`、`expires_at` 建立组合或单列索引。所有状态变更必须是条件更新，避免多 Worker 竞争覆盖。

## 11. 前端交互

前端对话编辑器新增图片选择、预览、移除和上传状态。发送前必须等待所有附件进入 `ready` 或 `partial`。

必须展示：

- 本地预览与文件大小。
- 上传、识别、完成、部分完成和失败状态。
- 图片校验与 Provider 错误的可操作中文提示。
- 用户消息中的缩略图，但不在 DOM 中暴露长期有效的对象 URL。
- Agent SSE 的附件加载、图片分析和最终回答阶段。

## 12. 生产部署基线

生产拓扑包含：

- Nginx/Ingress 与前端静态资源。
- BsAgent API。
- Image Analysis Worker。
- PostgreSQL。
- Redis。
- MinIO/S3。
- PaddleOCR Service。
- 外部 Vision API、RAG Service 和 MCP Servers。

`APP_ENV=production` 时必须 fail fast 拒绝：

- SQLite 数据库或 SQLite Checkpointer。
- Mock RBAC、Fake Vision/OCR Provider 或 Mock RAG。
- 空 JWT issuer、audience、JWKS URL。
- 空对象存储配置或公开 Bucket。
- 空 CORS Allowlist。
- 默认或弱密钥。

生产化同时包括：

- JWT/JWKS 验签与服务端 Principal。
- PostgreSQL LangGraph Checkpointer。
- Alembic 独立迁移任务。
- Redis 限流、短期记忆、缓存和 Celery Broker。
- `/health/live` 与 `/health/ready`。
- JSON 结构化日志，包含 `trace_id`、`run_id`、`tenant_id` 的脱敏标识。
- OpenTelemetry 导出接口，不绑定 LangSmith。
- Provider 超时、有界重试、熔断和并发限制。
- API、Worker、Frontend 和本地生产拓扑的 Dockerfile/Compose。
- 优雅停机、Worker 任务确认与未完成任务恢复。

## 13. 错误与降级

- OCR 成功、Vision 失败：`partial`，回答必须声明仅基于文字/表格识别。
- Vision 成功、OCR 失败：`partial`，数字和小字不得被当作高置信度事实。
- 两者都失败：`failed`，Agent Graph 在 `attachment_analyzer` 停止并返回结构化错误。
- Artifact 中 OCR 与 Vision 的数字冲突：保留冲突警告，Synthesizer 不自行选择其中一个作为确定事实。
- Asset 正在处理：Session API 返回 `asset_not_ready` 409，前端继续轮询状态。
- Asset 已删除、过期或跨租户：对外统一返回 404，避免泄露资源存在性。

## 14. 可观测性与审计

记录：

- Asset 上传、校验、重新编码、删除和过期。
- OCR/Vision Provider、模型版本、耗时、用量、状态和标准化错误码。
- Artifact 版本、分析 profile、置信度与警告数量。
- Graph 加载了哪些 `asset_id` 与 `analysis_id`。

默认不记录：

- 图片二进制或 Base64。
- 对象存储凭证和永久 URL。
- 完整 OCR 文本。
- Provider 的未脱敏原始响应。

## 15. 测试策略

### 15.1 单元测试

- 图片格式、Magic Bytes、大小、像素和解压炸弹校验。
- 重新编码与 EXIF 删除。
- S3/Local Asset Store Adapter 契约。
- Vision/OCR Provider 响应归一化。
- Artifact 合并、冲突、部分失败和置信度。
- 租户归属、外发策略和生产配置 fail fast。
- `attachment_analyzer` 对各 Asset 状态的路由。

### 15.2 集成测试

- PostgreSQL Asset/Analysis Repository 与 Alembic 迁移。
- Redis/Celery 幂等任务、重试和 Worker 恢复。
- MinIO 上传、读取、删除和过期。
- Fake Provider 下的图片到 Agent 最终回答全链路。
- JWT 身份、跨租户 Asset 和限流。
- SSE 事件顺序与错误契约。

### 15.3 端到端与真实服务测试

1. 财报截图可识别表格、公司和期间。
2. 纯图片消息也能进入正确 Intent。
3. K 线或折线图可输出趋势且标注视觉推断。
4. OCR 或 Vision 单边失败时输出 `partial` 和清晰边界。
5. 两个 Provider 都失败时 Agent 不得声称已看图。
6. 恶意文件、超限图片、跨租户 Asset 和过期 Asset 被拒绝。
7. Checkpoint 中只出现 Artifact 引用与摘要。
8. 前端桌面和移动视口完成上传、预览、删除、状态和回答流程。

## 16. 实施顺序

1. 先稳定并验证当前工作区已完成但未提交的审批恢复和 Mock RBAC 改动。
2. 建立 Asset Schema、Repository、Store SPI、上传安全链路和 API。
3. 建立 OCR/Vision Provider SPI、Artifact 合并器和异步 Worker。
4. 将 Attachment Analyzer 接入 Graph、Intent、Planner、Synthesizer 与 Trace。
5. 完成前端附件交互和 SSE 状态。
6. 补齐 JWT、PostgreSQL Checkpointer、Redis 限流、S3/MinIO 和生产配置校验。
7. 完成 Docker/Compose、迁移、健康检查、OpenTelemetry 接口和中文运维文档。
8. 使用真实图片、OCR 服务和 Vision API 完成上线验收。

## 17. 完成标准

以下证据全部成立后才可宣布可上线：

- 后端单元与集成测试全部通过。
- Ruff 和其它已配置静态检查通过。
- 前端类型检查与生产构建通过。
- Docker Compose 能从空环境启动全部必需服务。
- Alembic 可从空 PostgreSQL 升级到最新版本。
- 使用真实图片完成一次 OCR + Vision + Agent 回答全链路。
- 生产安全配置缺失时服务能够 fail fast。
- 跨租户、恶意上传、Provider 降级和数据过期测试通过。
- 中文架构、配置、部署、运维与故障排查文档与实际实现一致。
