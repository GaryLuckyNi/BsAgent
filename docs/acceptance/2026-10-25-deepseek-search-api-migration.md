# DeepSeek Search API 直连迁移验收

设计日期：2026-10-25。真实接口验证与评审完成日期：2026-10-26（北京时间）。

## 一、实现结果

- 默认 `web_search` 直连 DeepSeek `/v1/tools/search`，使用已有密钥和独立搜索地址；不请求搜索 Messages API，不增加辅助模型生成。
- 公共客户端协议和异常已从旧实现中提取。保留 `deepseek` 显式旧模式，失败不会自动回退。
- 工具更新为 `2.0.0`，查询限制为 128 字符，结果默认 5 条、最多 10 条；摘要优先取 `summary`，时间取合法 UTC `date`。
- 返回空 `answer`、未知 `coverage`，由现有研究节点依据来源继续判断证据满足程度。
- 新旧实现和非秘密配置进入本地执行绑定；保留可信研究上下文的注入通道，密钥轮换不改变绑定身份。
- 原始问题为可选的服务端上下文，超长或含现有策略能识别的内联凭证时整体省略，不冒充完整原问题、不把脱敏后的片段当原文发送。
- 普通执行、旧权限回退和批次恢复保留来源摘要及日期。既有内容预算继续生效，截断有明确标记。
- 直连观测是 API span，没有搜索 Generation 或虚构 token 用量。成功/失败机器统计在 `metadata_only` 模式下保留，查询及摘要被隐藏。

## 二、测试记录

先新增协议失败测试，观察到 30 项失败；实现客户端与公共契约后，相关 42 项通过。随后观察到配置、绑定及恢复证据共 10 项失败，再逐项补充实现，相关 225 项通过。

观测测试最初暴露统计字段被现有 Langfuse 元数据白名单过滤；修正测试对 SDK 扁平属性的读取方式，并增加专用 `search_*` 机器统计字段后，观测及既有内容策略共 29 项通过。

第一轮后端全量测试：

```text
.venv/bin/python -m pytest backend/app/tests -q --tb=short
2009 passed, 52 skipped, 2 warnings
```

该轮测试收集后才新增的真实接口测试与评审回归不计入上述数量。最终检查记录见本文末尾。

两个既有警告分别是 PostgreSQL 测试支持模块的断言重写提示、JWT 测试使用短合成 HMAC key 的警告，不是本轮真实凭证配置。

## 三、真实接口对比

探针读取 `.env.local` 和显式环境配置，不输出密钥。沙箱内第一次运行网络不可达，申请网络执行权限后重新运行成功。新旧模式分别对相同三条查询运行一次，不做多轮采样。

复现命令：

```bash
.venv/bin/python docs/acceptance/deepseek_search_api_live_probe.py --mode direct --output /tmp/bsagent-search-api-direct.json
.venv/bin/python docs/acceptance/deepseek_search_api_live_probe.py --mode legacy --output /tmp/bsagent-search-api-legacy.json
```

| 查询类型 | 直连耗时 | 旧模式耗时 | 直连输出 |
| --- | --- | --- | --- |
| 实体关系与招股资料 | 1,030 ms | 5,607 ms | 3 条来源，含摘要与发布时间 |
| 2026-10-23 A 股收盘新闻 | 354 ms | 6,164 ms | 3 条来源，含摘要与发布时间 |
| 贵州茅台指定日期估值资料 | 366 ms | 3,602 ms | 3 条来源，含摘要与发布时间 |

三条直连结果的 `answer` 均为空、`coverage` 均未知、`usage` 均为空字典；旧模式生成了总答复并返回模型用量，其返回来源在本次样本中没有摘要和发布时间。新旧来源不要求逐项一致，独立 API 返回的摘要并不等同于原文或专业数据接口验证。

有界公开来源及原始观测值保存在：

- [直连样本](2026-10-25-deepseek-search-api-migration/direct.json)
- [旧模式样本](2026-10-25-deepseek-search-api-migration/legacy.json)

探针每次直接发起一条客户端搜索调用；旧模式内部服务端搜索次数以返回的 `usage.server_tool_use` 为准，不能将一条客户端调用等同于一次内部搜索。样本不是稳定延迟基线或通用质量证明，不据此承诺固定提速比例。

### 隔离的真实执行与合成节点

额外运行：

```bash
.venv/bin/python docs/acceptance/deepseek_search_api_live_probe.py --agent --output /tmp/bsagent-search-api-agent.json
```

使用真实 DeepSeek 执行模型、实际 `ToolCandidateResolver`、`ToolExecutionGateway`、Search API 和合成节点；注册表仅开放 `web_search`，不连接生产会话或其他 MCP。模型自行生成了符合限制的估值查询及 `max_results=8`，实际搜索成功返回 8 条来源，空 `answer` 和未知覆盖状态被正确接收，合成节点读取来源摘要并生成包含数据口径、时间和限制的答复。整体耗时 6,994 ms。

节点探针未配置记忆 Store，出现一次非致命的 `experience memory recall failed` 日志，随后按现有回退机制完成合成；未为此修改生产记忆策略。证据见 [隔离节点样本](2026-10-25-deepseek-search-api-migration/agent.json)。

本探针没有执行 Router、Planner、持久化调度、真实权限确认或生产 HTTP 会话，不作为完整生产研究会话或金融数据事实独立核验。

## 四、独立评审与修复

独立评审只读检查当前 diff、设计、测试及纯内存复现，没有操作真实服务。

1. 旧权限回退超过内容预算的压缩分支会丢弃日期。先补充用长摘要触发该分支的测试，观察 `published_at` 缺失，再补齐日期，回归通过。
2. `ori_question` 新增可信上下文通道可能绕过工具参数中的内联凭证检测。先复现 API Key/Bearer 原文被发送，随后补充 password/token/client_secret 三类失败用例；复用既有凭证检测和脱敏策略，检测或脱敏发现敏感内容时整体省略该字段。相关 72 项测试通过，最终独立复核未发现剩余新增 P1/P2。

## 五、明确的验证边界

- 已验证真实 Search 与旧 Messages 搜索接口，没有执行完整的真实生产研究会话、真实权限确认恢复或生产部署。离线图级回归与接口探针不能替代这些验收。
- 默认测试跳过真实外部服务和专用数据库环境检查；未配置的 PostgreSQL 并发检查不算通过。
- 恢复查询来自冻结目标及验收条件，超出 128 字符时明确参数失败、无远端请求，不静默裁剪或新增模型重写。调用方需要生成符合新契约的短查询。
- 既有持久回执清理可能将单字段超过 2,048 字符的内容截断，完整性保护会阻止把这种回执当成完整结果复用。允许 4,000 字符的搜索摘要可能触发此边界；本轮保持保护规则，不通过重复远端请求或放松完整性校验规避。
- Fetch、自动正文抓取、工具内部循环均未接入。

## 六、最终检查

在全部业务代码和评审修复完成后重新执行后端全量测试：

```text
.venv/bin/python -m pytest backend/app/tests -q --tb=short
2015 passed, 53 skipped, 2 warnings in 196.03s
```

修改涉及的 Python 文件、独立接口测试和验收探针均通过 Ruff，`git diff --check` 通过。53 项跳过包含显式关闭的真实服务测试及缺少专用环境的集成检查；真实 Search 和隔离节点验证另由上述探针完成。

保留分支 `codex/deepseek-search-api` 供后续集成，不自动合并、推送或部署。设计、实现、确定性验证、真实接口对比和独立评审已完成，Fetch 不属于本阶段交付。
