> 历史归档，不代表当前配置。

# Langfuse Cloud EU 真实 smoke 验收

## 结果

2026-09-26，在 `codex/feature-langfuse-cloud-poc` 分支完成。按用户更正使用 **EU**（`https://cloud.langfuse.com`），不是 Japan。最终真实运行退出码为 **0**，16 项验收断言全部通过。

- [云端 trace](https://cloud.langfuse.com/project/cmugaat1m02sgad0cutels028/traces/ded57b0a1d77ec1892dc682efd8f3493)
- business_trace_id：`trace_9c36c463086644c79a1d5e0bdcd912f5`
- Langfuse trace ID：`ded57b0a1d77ec1892dc682efd8f3493`
- AgentRun：`run_fe2b263fba07f6155193cf8ed3442f21f6695271e48e6e6d800a895dde54`，本地状态 `completed`。
- [脱敏 JSON 证据](2026-09-26-langfuse-eu-smoke.json)：仅保存标识、计数、耗时、成本、层级与布尔断言，不保存配置值、prompt 或回答正文。

## 层级和隐私

共 14 个 observation，只有一个 `bs-agent.run` 业务根。所有子 observation 均能沿 parent ID 回溯到该根。

```text
bs-agent.run
├── LangGraph
│   ├── catalog_guard / attachment_analyzer
│   ├── intent_router
│   │   └── deepseek.chat.completions
│   └── synthesizer / verifier / approval_coordinator / final_response
└── smoke.privacy_probe
    ├── smoke_echo（真实本地工具调用）
    └── deepseek.chat.completions（thinking 开启）
```

根 metadata 的 `business_trace_id` 与 AgentRun 一致，`execution_segment=initial`，业务节点的 session ID 与命名空间 SHA-256 摘要一致。探针是业务 Graph 完成后显式创建的独立子树，不是业务 Agent 自动选中的工具。

所有 observation 的 input/output 均为空；在完整云端回读字段中扫描实际 prompt、回答、非空 reasoning、工具输入/结果、唯一 canary、原始会话标识和 secret，未发现匹配。短答案按字段精确匹配，避免与 UUID 中的数字碰撞。SDK 标准 scope 中的项目 public key 是项目归属标识，不是 secret；验收过程与证据均未打印它。

## DeepSeek usage 与成本

模型均为 `deepseek-v4-flash`。usage 与本地实际 provider 返回逐项一致；云端成本与模型定义的算式一致。

| 调用 | input | output | total | 云端估算 USD | latency 秒 | TTFT 秒 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 业务意图识别 | 223 | 31 | 254 | 0.00005205 | 0.693 | 0.540 |
| reasoning 隐私探针 | 75 | 55 | 130 | 0.00004425 | 0.897 | 0.720 |

EU 项目原先未提供可匹配的 DeepSeek 价格定义，Generation 有 usage 但 `totalCost=null`。已新增精确匹配 `(?i)^deepseek-v4-flash$` 的项目模型定义 `cmuihd9680m5oad0d0ffzo5kj`，生效时间 `2026-09-26T00:00:00Z`，tier 名为 `PoC 2026-09-26 off-peak cache-miss estimate`。

依据 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)，当天为周六，使用非高峰输入未命中价 $0.15/百万 token、输出价 $0.60/百万 token。项目仍仅上传 input/output/total，未拆缓存桶，所以这是 **PoC 估算，不是供应商账单**。该静态配置不会自动适配后续高峰时段或调价，后续使用需更新模型定义。业务代码没有硬编码单价。

## 修复与回归

1. 脱敏补齐 trace 级 input/output、callback metadata 白名单与稳定错误码过滤，防止工具数据或原始异常绕过 observation I/O 脱敏。
2. 修复 `reasoning_chars` 被误删，保留安全计数。
3. 新增可复现脚本，使用官方 Observations API v2 完整回读；旧 trace API 本次返回 410。
4. 回归覆盖脱敏边界、v2 查询字段/时间范围、拒绝不完整分页、短答案扫描误报。相关测试 **116 passed**，Ruff 与 `git diff --check` 通过。

## 复现

在仓库根目录执行，先确认 `.env.local` 使用 EU 项目 keys 和 DeepSeek key，避免将密钥放到命令行参数：

```bash
.venv/bin/python scripts/langfuse_smoke.py --region eu
.venv/bin/python scripts/langfuse_smoke.py --region eu --run

/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_langfuse_smoke.py \
  backend/app/tests/unit/test_langfuse_observability.py \
  backend/app/tests/unit/test_deepseek_client.py \
  backend/app/tests/integration/test_dynamic_task_graph.py \
  backend/app/tests/unit/test_config.py \
  backend/app/tests/unit/test_lifecycle.py -q
```

本机 `.venv` 包含运行依赖但没有 pytest，因此回归使用已安装 pytest 的 `/opt/anaconda3/bin/python3`。其他环境可使用安装了项目 dev 依赖的 Python。

本次真实业务服务和生产 Graph 在进程内 SQLite 上运行；**不覆盖 PostgreSQL 持久化、HTTP API、审批恢复或生产基础设施**。本地测试数据随进程退出释放，云端 trace 保留用于复核。排障期间另有早期 trace，最终验收以本文指定 ID 为准。
