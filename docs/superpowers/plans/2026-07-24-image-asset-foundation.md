# 生产身份与图片 Asset 基础实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 建立可上线的身份边界与安全图片资产底座，使后续 OCR、Vision 和 LangGraph 只能通过受控 Asset 引用消费图片。

**架构：** FastAPI 使用统一 `Principal` Dependency 解析可信身份；Asset Service 对上传图片执行有界读取、真实格式识别、解码重编码、EXIF 清理、租户归属和生命周期管理；二进制通过 `ObjectStore` SPI 写入本地或私有 S3/MinIO，元数据通过 PostgreSQL Repository 管理，分析任务仅通过端口触发。

**技术栈：** Python 3.11、FastAPI、Pydantic v2、PyJWT、Pillow、filetype、SQLAlchemy、Alembic、PostgreSQL、boto3、pytest

---

## 文件地图

- 身份：`backend/app/security/principal.py`、`backend/app/security/jwt_provider.py`、`backend/app/security/dependencies.py`
- 图片资产：`backend/app/assets/schemas.py`、`image_security.py`、`object_store.py`、`local_store.py`、`s3_store.py`、`repository.py`、`service.py`、`factory.py`
- 接口：`backend/app/api/assets.py`、`backend/app/api/sessions.py`、`backend/app/main.py`
- 配置：`backend/app/core/config.py`、`pyproject.toml`
- 数据库：`backend/app/storage/models.py`、`alembic/versions/20260724_0005_asset_records.py`
- 测试：`backend/app/tests/unit/test_jwt_provider.py`、`test_asset_repository.py`、`test_image_security.py`、`test_object_store.py`、`test_asset_service.py`、`backend/app/tests/integration/test_asset_api.py`、`test_session_api.py`

### Task 1：收口审批恢复与 Mock RBAC 基线

**文件：**
- 修改：`backend/app/tests/integration/test_graph_runtime.py`
- 修改：`backend/app/tests/integration/test_session_api.py`
- 修改：`docs/checkpoint.md`
- 修改：`docs/tool-agent.md`

- [ ] 运行现有审批、RBAC、Tool Gateway、Graph Runtime 和 Session API 定向测试，记录当前基线。
- [ ] 补充“审批后工具参数发生变化时拒绝恢复”的失败测试，断言旧哈希不能授权新调用。
- [ ] 补充跨租户读取审批、重复决策和拒绝后恢复的测试；实现只修正测试暴露的问题。
- [ ] 更新中文文档，明确业务 `ApprovalRecord`、LangGraph checkpoint 与 `AgentRun` 的职责边界。
- [ ] 运行完整后端测试、Ruff 和 `git diff --check`，将审批恢复/RBAC 作为独立提交。

**验证命令：**

```bash
/opt/anaconda3/bin/python3 -m pytest -q backend/app/tests/unit/test_approval_repository.py backend/app/tests/unit/test_rbac.py backend/app/tests/unit/test_tool_agent.py backend/app/tests/integration/test_graph_runtime.py backend/app/tests/integration/test_session_api.py
/opt/anaconda3/bin/python3 -m pytest -q
/opt/anaconda3/bin/python3 -m ruff check backend
git diff --check
```

### Task 2：依赖、环境模式与生产配置门禁

**文件：**
- 修改：`pyproject.toml`
- 修改：`backend/app/core/config.py`
- 测试：`backend/app/tests/unit/test_config.py`

- [ ] 先编写失败测试：`app_env=production` 时禁止 Mock 身份、Local Object Store、SQLite 和缺失的 JWT/S3 配置。
- [ ] 增加 `PyJWT[crypto]`、`Pillow`、`filetype`、`python-multipart` 和 `boto3` 依赖，并在 `pyproject.toml` 中设置兼容版本下限。
- [ ] 增加 `app_env`、上传限制、允许格式、保留天数、对象存储后端、S3 endpoint/bucket/region、JWT issuer/audience/JWKS URL 或公钥配置。
- [ ] 使用 Pydantic Settings 模型校验器实现 fail-fast；生产非法配置必须在应用启动时失败并给出具体字段错误。
- [ ] 保持测试环境默认值可离线运行，禁止业务代码通过隐式 fallback 降级到 Mock。

**关键测试：**

```python
def test_production_rejects_mock_identity_and_local_asset_store():
    with pytest.raises(ValidationError):
        Settings(app_env="production", auth_provider="mock", asset_store_backend="local")
```

### Task 3：统一 Principal Dependency 与 JWT 验证

**文件：**
- 修改：`backend/app/security/rbac.py`
- 新建：`backend/app/security/principal.py`
- 新建：`backend/app/security/jwt_provider.py`
- 新建：`backend/app/security/dependencies.py`
- 修改：`backend/app/main.py`
- 测试：`backend/app/tests/unit/test_jwt_provider.py`
- 测试：`backend/app/tests/integration/test_session_api.py`

- [ ] 先编写失败测试，覆盖有效 token、错误签名、错误 issuer/audience、过期 token、缺失 `sub`、缺失 `tenant_id` 和非法 scopes。
- [ ] 定义不可变 `Principal(user_id, tenant_id, scopes, token_id)`，统一替代接口 body/header 中不可信的用户与租户字段。
- [ ] 实现 `JwtPrincipalProvider`，验证允许算法、签名、`exp`、issuer、audience 和必需 claims；JWKS 按 `kid` 缓存并设置刷新上限。
- [ ] 实现 FastAPI `HTTPBearer` Dependency，将认证失败统一映射为不泄露内部原因的 `401`，权限不足映射为 `403`。
- [ ] 保留显式 `MockPrincipalProvider` 供测试和本地开发使用；配置为生产环境时构建工厂必须拒绝它。
- [ ] 修改 Session/Approval API 从 `Principal` 获取身份，并保留兼容测试所需的 Dependency Override，不在 endpoint 参数中暴露数据库 Session。

### Task 4：Asset 数据模型、迁移与 Repository

**文件：**
- 修改：`backend/app/storage/models.py`
- 新建：`backend/app/assets/__init__.py`
- 新建：`backend/app/assets/schemas.py`
- 新建：`backend/app/assets/repository.py`
- 新建：`alembic/versions/20260724_0005_asset_records.py`
- 测试：`backend/app/tests/unit/test_asset_repository.py`

- [ ] 先编写失败测试，覆盖创建、按租户读取、跨租户不可见、内容哈希查询、过期筛选、软删除和条件状态迁移。
- [ ] 新增 `asset_records`：`asset_id`、`tenant_id`、`user_id`、`session_id`、`object_key`、`sha256`、`media_type`、`size_bytes`、`width`、`height`、`status`、`expires_at`、时间戳和删除时间。
- [ ] 为 `(tenant_id, asset_id)`、`(tenant_id, sha256, status)`、`expires_at` 建立与查询一致的索引；对象 key 不允许作为公开 API 标识。
- [ ] 定义 `uploading|processing|ready|partial|failed|expired|deleted` 枚举和允许的状态转换矩阵。
- [ ] Repository 的状态更新必须携带期望旧状态，在单条条件 SQL 中完成，竞争失败返回领域冲突而不是静默覆盖。
- [ ] Alembic `down_revision` 指向 `20260713_0004`，验证从空库升级到 head 及 downgrade/upgrade 本迁移。

**关键测试：**

```python
def test_asset_repository_never_reads_another_tenants_asset(session):
    repo = SqlAlchemyAssetRepository(session)
    asset = repo.create(make_asset(tenant_id="tenant-a"))
    assert repo.get(asset.asset_id, tenant_id="tenant-b") is None
```

### Task 5：图片安全校验与规范化

**文件：**
- 新建：`backend/app/assets/image_security.py`
- 新建：`backend/app/assets/errors.py`
- 测试：`backend/app/tests/unit/test_image_security.py`
- 测试夹具：`backend/app/tests/fixtures/images/`

- [ ] 使用 Pillow 在测试中生成 JPEG、PNG、WEBP、带 EXIF 图片、超像素图片、动图和伪造扩展名文件，先观察拒绝测试失败。
- [ ] 对上传流执行有界读取，超过 `max_image_bytes` 立即停止；不得先将无界 body 全量读入内存。
- [ ] 使用 `filetype` 检测 Magic Bytes，再由 Pillow 完整 `verify`、重新打开和 `load`，声明类型、检测类型与解码格式必须一致。
- [ ] 拒绝 SVG、GIF、动图、多帧图片、超像素图片、零尺寸和 Pillow decompression bomb；仅允许 JPEG、PNG、WEBP。
- [ ] 将图片转换到允许色彩模式后重新编码，移除 EXIF、ICC 中的非必要敏感元数据和尾随数据，再计算规范化内容的 SHA-256。
- [ ] 定义稳定错误码：`asset_too_large`、`unsupported_image_type`、`image_decode_failed`、`image_too_many_pixels`、`animated_image_not_allowed`。

**关键测试：**

```python
def test_sanitizer_removes_exif_and_ignores_claimed_extension():
    sanitized = sanitize_image(make_jpeg_with_gps_exif(), claimed_media_type="image/png")
    assert sanitized.error_code == "unsupported_image_type"
```

### Task 6：ObjectStore SPI、Local 与私有 S3 实现

**文件：**
- 新建：`backend/app/assets/object_store.py`
- 新建：`backend/app/assets/local_store.py`
- 新建：`backend/app/assets/s3_store.py`
- 测试：`backend/app/tests/unit/test_object_store.py`

- [ ] 先编写契约测试，覆盖 put/get/delete、重复 delete 幂等、路径穿越拒绝、S3 私有写入参数和异常映射。
- [ ] 定义 `ObjectStore` Protocol，只接受受控 object key 和 bytes/stream，不向业务层泄露 boto3 类型。
- [ ] `LocalObjectStore` 仅用于测试和开发，使用解析后的根目录校验防止 `../`、绝对路径和符号链接逃逸。
- [ ] `S3ObjectStore` 使用私有 bucket、服务端加密配置、连接/读取超时和有限重试；禁止 `public-read` ACL。
- [ ] 单元测试注入 fake S3 client 验证调用参数，不依赖网络；真实 MinIO 连通性留到生产 Compose 集成测试。
- [ ] object key 使用不可猜测 ID 与租户分区，但所有读取仍必须先经过数据库租户归属校验。

### Task 7：AssetService、去重、补偿与分析任务端口

**文件：**
- 新建：`backend/app/assets/service.py`
- 新建：`backend/app/assets/factory.py`
- 测试：`backend/app/tests/unit/test_asset_service.py`

- [ ] 先编写失败测试：正常上传、同租户去重、跨租户不去重、对象写入失败、数据库提交失败补偿、查询、软删除和过期资产拒绝。
- [ ] 定义 `ImageAnalysisDispatcher` Protocol；Phase 1 使用显式 `NoopImageAnalysisDispatcher` 仅允许测试，应用环境不允许伪造 `ready` 分析结果。
- [ ] 上传顺序为“净化 -> 创建 uploading 元数据 -> 私有对象写入 -> 原子更新 processing -> 提交 -> 分发任务”，失败时执行可观测的对象/记录补偿。
- [ ] 去重范围限制在同一租户与相同规范化 SHA-256，复用对象时维护独立 Asset 归属记录，避免泄露其他用户是否上传过同一图片。
- [ ] `get/delete` 必须同时匹配 `asset_id + tenant_id`；删除采用软删除并触发幂等对象清理。
- [ ] 分发失败不得回滚已经安全保存的资产，将状态置为 `failed` 并记录稳定原因码，供后续重试。
- [ ] 所有日志只包含 asset/run/tenant 哈希化标识、状态和耗时，不输出图片、Base64、EXIF 或完整文件名。

**关键测试：**

```python
def test_upload_compensates_object_when_database_commit_fails():
    store = RecordingObjectStore()
    service = make_service(store=store, repository=FailingCommitRepository())
    with pytest.raises(AssetPersistenceError):
        service.upload(make_upload(), principal=principal())
    assert store.deleted_keys == store.written_keys
```

### Task 8：鉴权 Asset API 与 Session 附件契约

**文件：**
- 新建：`backend/app/api/assets.py`
- 修改：`backend/app/api/sessions.py`
- 修改：`backend/app/main.py`
- 测试：`backend/app/tests/integration/test_asset_api.py`
- 测试：`backend/app/tests/integration/test_session_api.py`

- [ ] 先编写失败测试：未认证上传、有效上传、超限、非法图片、跨租户查询/删除、重复删除和消息引用无归属附件。
- [ ] 实现 `POST /api/assets/images` multipart 上传，成功返回 `202` 与 `asset_id/status`；校验错误返回稳定 `422` 领域码。
- [ ] 实现 `GET /api/assets/{asset_id}` 元数据查询和 `DELETE /api/assets/{asset_id}` 软删除；跨租户统一表现为 `404`，避免存在性泄露。
- [ ] Endpoint 仅依赖 `Principal`、`AssetService` 和 UoW/Session Dependency，禁止请求体传入可信 `tenant_id/user_id`，禁止将数据库连接作为公开 API 参数。
- [ ] 将 Session 消息请求收敛为 `message + attachment_ids`；后端校验附件属于当前租户/用户/会话且状态允许消费。
- [ ] 每条消息最多 4 个附件，去重后再校验；原始图片、对象 key 和 Base64 不进入 `AgentState`、checkpoint、SSE 或 trace。
- [ ] 为附件未就绪返回 `409 asset_not_ready`，为已失败返回 `422 asset_analysis_failed`，契约为 Phase 2/3 保留扩展字段。

**接口示例：**

```json
{
  "message": "分析这张利润表中的收入和毛利变化",
  "attachment_ids": ["ast_01J..."]
}
```

### Task 9：迁移验证、中文文档与 Phase 1 门禁

**文件：**
- 新建：`README.md`
- 新建：`docs/image-assets.md`
- 新建：`docs/security.md`
- 新建：`docs/api.md`

- [ ] 用中文记录上传安全链路、数据归属、保留/删除语义、Local 与 S3 适用环境和稳定错误码。
- [ ] 记录 JWT claims 契约、JWKS 轮换行为、Principal 注入方式和生产 fail-fast 项。
- [ ] 从空数据库执行 `alembic upgrade head`，再 downgrade/upgrade Asset 迁移，确认索引和约束一致。
- [ ] 运行 Asset/JWT/Session 定向测试，再运行完整后端测试和 Ruff。
- [ ] 使用 `git diff --check` 检查全部改动，不提交二进制测试样本、密钥、上传文件或本地数据库。
- [ ] 对照设计文档 Phase 1 条目逐项提供测试或命令证据，全部成立后独立提交。

**最终验证命令：**

```bash
/opt/anaconda3/bin/python3 -m pytest -q backend/app/tests/unit/test_config.py backend/app/tests/unit/test_jwt_provider.py backend/app/tests/unit/test_asset_repository.py backend/app/tests/unit/test_image_security.py backend/app/tests/unit/test_object_store.py backend/app/tests/unit/test_asset_service.py backend/app/tests/integration/test_asset_api.py backend/app/tests/integration/test_session_api.py
/opt/anaconda3/bin/python3 -m pytest -q
/opt/anaconda3/bin/python3 -m ruff check backend
alembic upgrade head
git diff --check
```

## Phase 1 完成标准

只有同时满足以下条件，才能进入 OCR + Vision 实现：

1. 生产配置无法使用 Mock Principal、Local Object Store 或 SQLite 启动。
2. 未认证、跨租户、超限、伪造类型、动图和超像素上传均被稳定拒绝。
3. 存储中的图片已重编码且不含 EXIF，bucket/object 默认私有。
4. Agent、checkpoint、trace 和 SSE 中不存在原始图片、Base64 或对象存储凭据。
5. 迁移、定向测试、完整测试、Ruff 和 diff check 全部通过。
