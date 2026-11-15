# 图片资产、后台身份与 OCR/Vision 内容边界验收

日期：2026-11-03。范围为 BsAgent 维护修复，仅本地实现和测试；未提交、部署或迁移，未修改独立 RAG 工程。

## 本批结果

修复同租户他人默认可通过资产详情/分析/删除路径访问资源的问题；图片任务以持久化身份绑定和当前权限执行。OCR/Vision 文本、结构化结果、artifact、缓存及上下文进入共享凭证检查。源图片像素不由文本规则改写。

| 边界 | 当前行为 |
| --- | --- |
| 资产详情、分析与删除 | 默认仅 owner；`asset:read:any` 和 `asset:delete:any` 分别允许同租户相应操作，单独授予即可，不互相替代；跨租户不放行。HTTP 与服务层契约一致 |
| 图片后台任务 | 核对 asset/analysis/tenant/user/session 持久化绑定和有效期；在原图/缓存对象读取、OCR、Vision 及结果保存前重新核对当前 `asset:write`；不能从队列复制权限 |
| 目录故障/撤权 | 固定拒绝或暂不可用错误，无原始异常链；不调用后续 provider，不借拒绝改写他人的资产。已完成的前一次 OCR 请求不被声称为未执行 |
| OCR/Vision 客户端与协调器 | 完整文本/表格/usage 检查后返回；先检查再截断，不能靠 excerpt 截断留下半份凭证；HTTP、解析、超深 JSON 与熔断错误采用固定错误，不保留原传输链 |
| 原像素的后续 Vision 外发 | OCR 发现已知凭证或有限注入信号时，跳过后续 Vision，保留安全 OCR 部分结果和固定风险标志。检测标记为本地 PrivateAttr，HTTP 返回不能伪造或清除 |
| 持久化/对象 JSON | 新结果和 artifact 保存安全副本；共享检查故障不保存未检查内容。正在 processing 的本方任务可标失败并按既有机制重试，已完成状态不因读取故障伪装成未执行 |
| 旧行与缓存 | 读取 ORM 行返回瞬态安全副本，不改旧数据；缓存记录与 artifact 在复制前重查。旧格式损坏可作为 miss 重算，检查器故障不能触发 fallback 外发 |
| API/模型上下文 | 重新保护后返回，保留角色、调用身份与普通数字；图片上下文附加宿主生成的 `source_trust=untrusted_image_data`，内容不构成用户授权 |

安全内核为 `security/asset_authorization.py`、`security/image_content.py`；适配位于资产服务/API、Vision 公共边界及仓储。通用 `require_permission` 只在调用点显式传入 alternatives 时接受替代 scope，默认行为不扩大。业务 Agent 不需要增加逐次安全调用。

## 测试与复核

- 新专项 **42 passed**：[日志](2026-11-03-image-asset-unit-tests.log)、[JUnit](2026-11-03-image-asset-unit-tests.xml)。使用合成 canary、内存数据库、MockTransport、真实 FastAPI 路由与真实权限目录，不调用外部 OCR/Vision 模型。
- 关联集合 101 个文件：**1807 passed、6 skipped**，见 [日志](2026-11-03-image-asset-tests.log)、[JUnit](2026-11-03-image-asset-tests.xml)和[文件清单](2026-11-03-image-asset-test-files.txt)。专项与关联统计重叠，不相加。跳过项仍为 5 项缺少专用 PostgreSQL 地址的迁移测试及 1 项未启用的真实 DeepSeek 质量评测，不计通过。
- 独立只读复核发现并修复 `:any` 在 HTTP/服务层不一致；复验 **47 passed**，未发现所审范围剩余 P1/P2；另验证默认依赖不隐式接受 `:any`。
- 新安全文件及其余受影响核心文件 Ruff 通过。资产 API/服务仍有 17 条 HEAD 已存在的检查项（15 条 B008、RUF012、BLE001），比较确认没有新增；不宣称整个项目静态检查全绿。
- `git diff --check` 与交付文档本地链接核对通过。

先观察失败后修复的主要场景：20 项初始身份/数据负例；OCR 后撤权未阻断第二 provider；检查故障被当作 cache miss；HTTP `:any` 正向被拒；两个 provider 的 1500 层 JSON 未归一化。保留同租户正常 owner、显式按操作授权、真实目录未撤权、普通财务字段、缓存命中与故障重试对照。

复跑：

```sh
xargs .venv/bin/python -m pytest -q --tb=short < docs/security/reports/2026-11-03-image-asset-test-files.txt
```

## 仍需验收

1. OCR 自身接收图片像素发生在文字检查之前。本批不能证明图片中所有秘密都能被 OCR 识别，也不证明首个 OCR 服务属于可信部署。OCR 未识别/服务失败且租户允许时，原有 Vision 降级路径仍可使用；必须另行定义图片类别、接收方及本地/外部部署策略。
2. 注入模式只提供有限风险信号。信任标记和跳过后续 Vision 不等于真实多模态模型或下游模型已经防住任意诱导、编码及多轮洗白。
3. 单字符串检查上限 250,000 字符、总量 2,000,000 字符、50,000 节点、32 层；不代表解码前响应大小、全任务预算、并发及所有 HTTP 资源约束已完成。
4. 历史数据库/对象原文未批量清理；旧数据读取和复制保护不等于历史秘密已删除。其它持久化、业务拼接链接/卡片、独立观测 handler/metrics label 继续专项验收。
5. 身份目录与用户停用可阻止下一次调用，不承诺原子撤回已发出的请求；IdP 撤销同步、Celery 队列和部署准入、实际 OCR/Vision 配置及真实跨租户服务授权仍需环境证据。

SEC-03/05/07/09/10 的总项继续保留，不因本批局部实现勾选全部 Harness 安全。

## 外部 RAG 的额外只读证据

本轮发现用户提供的 8011 正在监听，读取 `http://127.0.0.1:8011/openapi.json` 返回 200，服务标题为 Minimal RAG API，公开契约未声明 securitySchemes/global security，检索操作也未单独声明认证要求。[脱离业务文档的契约摘要](2026-11-03-rag-openapi-contract.json)保留该事实。

这不足以证明没有隐藏中间件/代理认证，也没有证明文档 ACL。没有匿名执行真实检索、读取知识文档或修改外部服务。下一步仍需服务认证、可信委派及文档可见性契约；不能用 trace Header、普通 filters 或客户端补字段冒充下游授权完成。
