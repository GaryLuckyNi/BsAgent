# Langfuse Cloud 最小观测验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将本地 BsAgent 的一次 AgentRun 安全地发送到 Langfuse Cloud Japan，能查看 Agent/Graph 层级、LLM 模型、时延、Token、成本和会话聚合，同时不上传 prompt、回答正文、reasoning 或密钥。

**Architecture:** 保留 PostgreSQL 业务观测表作为审计与排障事实源；新增一个很薄的 Langfuse 适配层，直接使用官方 Python SDK、官方 LangChain/LangGraph `CallbackHandler`、官方 `mask_otel_spans` 和官方 Generation observation。Langfuse 使用独立 `TracerProvider`，避免与项目现有通用 OpenTelemetry provider 冲突；SDK 自己负责批处理、重试和成本推断，项目只负责配置、生命周期、关联字段和隐私边界。

**Tech Stack:** Python 3.11、FastAPI、LangGraph、LangChain、Langfuse Python SDK v4、OpenTelemetry、Pydantic Settings、Node.js 内置 `.env` 加载能力、pytest、Node test runner。

---

## 实施边界

- 本阶段只接 Langfuse Cloud Japan，不新增独立前端、观测数据库、消息队列、重试器或价格表。
- PostgreSQL 中的 `agent_runs`、`agent_trace_events`、`agent_trace_artifacts` 继续承担业务状态、审计、审批恢复和受控排障职责。
- Langfuse 只接收结构化白名单字段；不发送 prompt、用户问题、回答正文、reasoning、工具参数/结果、请求头、密钥或原始异常文本。
- 本阶段不启用生产环境、不增加 readiness 依赖、不接 Prompt Management、Dataset、Evaluation 或告警。
- 成本由 Langfuse 的模型定义推断。若 Cloud Japan 尚未内置当前 DeepSeek 模型，只在 Langfuse Project Settings 中增加模型定义，不在业务代码里硬编码价格。

## Task 1：本地环境变量入口

**Files:**
- Create: `scripts/local-env.mjs`
- Create: `scripts/local-env.test.mjs`
- Create: `.env.example`
- Modify: `.gitignore`
- Modify: `scripts/dev.mjs`
- Modify: `package.json`

- [ ] **Step 1：先写失败测试，锁定 `.env.local` 的加载规则**

使用 Node 内置 test runner 创建临时 env 文件，覆盖三种行为：文件不存在时无副作用、文件存在时加载、已有 shell 环境变量优先。

```js
test('loads .env.local without overriding shell variables', () => {
  process.env.BS_AGENT_LANGFUSE_ENABLED = 'from-shell'
  loadLocalEnvironment(envFile)
  assert.equal(process.env.BS_AGENT_LANGFUSE_ENABLED, 'from-shell')
  assert.equal(process.env.BS_AGENT_LANGFUSE_PUBLIC_KEY, 'pk-local')
})
```

Run: `node --test scripts/local-env.test.mjs`

Expected: FAIL，提示 `scripts/local-env.mjs` 不存在。

- [ ] **Step 2：实现最小加载器并在开发脚本最前面调用**

```js
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import process from 'node:process'

export function loadLocalEnvironment(file = resolve('.env.local')) {
  if (!existsSync(file)) return false
  process.loadEnvFile(file)
  return true
}
```

`scripts/dev.mjs` 必须在读取任何 `process.env` 之前调用 `loadLocalEnvironment()`，这样后续派生的 backend 子进程自动继承这些值。不要引入 `dotenv` 依赖。

- [ ] **Step 3：增加示例文件和忽略规则**

`.env.example` 只写 MVP 所需字段及 Japan 默认值说明：

```dotenv
BS_AGENT_LANGFUSE_ENABLED=false
BS_AGENT_LANGFUSE_PUBLIC_KEY=
BS_AGENT_LANGFUSE_SECRET_KEY=
# 可选；默认 https://jp.cloud.langfuse.com
# BS_AGENT_LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com
```

`.gitignore` 增加 `.env.local`，不得忽略 `.env.example`。

- [ ] **Step 4：验证并提交**

Run: `npm run test:dev-env`

Expected: PASS。

Run: `git diff --check`

Expected: 无输出。

```bash
git add package.json scripts/dev.mjs scripts/local-env.mjs scripts/local-env.test.mjs .env.example .gitignore
git commit -m "chore: load local development environment"
```

## Task 2：Langfuse 依赖与安全配置

**Files:**
- Modify: `pyproject.toml`
- Modify: `backend/requirements.lock`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/tests/unit/test_config.py`
- Modify: `backend/app/tests/deployment/test_container_manifests.py`

- [ ] **Step 1：先写配置失败测试**

新增测试覆盖：默认关闭、Japan URL 与 100% 本地采样默认值、启用时缺任一 key 立即失败、采样率超界失败、非 HTTPS base URL 失败。

```python
def test_langfuse_requires_both_keys_when_enabled():
    with pytest.raises(ValidationError, match="Langfuse"):
        Settings(
            langfuse_enabled=True,
            langfuse_public_key="pk-test",
            langfuse_secret_key="",
        )
```

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py -q`

Expected: FAIL，因为字段和校验尚未实现。

- [ ] **Step 2：增加配置字段和校验**

在 `Settings` 中增加：

```python
langfuse_enabled: bool = False
langfuse_public_key: str = ""
langfuse_secret_key: str = ""
langfuse_base_url: str = "https://jp.cloud.langfuse.com"
langfuse_sample_rate: float = Field(default=1.0, ge=0, le=1)
```

校验规则：仅在 `langfuse_enabled=True` 时要求 public/secret key；base URL 必须是 HTTPS。`environment` 直接使用现有 `app_env`，不再增加一个重复变量。错误文本只写缺失字段名，不回显值。

- [ ] **Step 3：使用官方 SDK v4 并更新可复现锁文件**

在 `pyproject.toml` 增加 `langfuse>=4.15.4,<5.0.0`。使用 Python 3.11 的干净虚拟环境和 `pip-tools` 解析项目依赖并重新生成 `backend/requirements.lock`；保留锁文件顶部现有中文说明，确保锁文件含 `langfuse==4.*`，不手工拼接传递依赖。

```bash
python3.11 -m venv /tmp/bs-agent-langfuse-lock
/tmp/bs-agent-langfuse-lock/bin/python -m pip install "pip-tools>=7.5,<8"
/tmp/bs-agent-langfuse-lock/bin/pip-compile --strip-extras --no-header --output-file /tmp/bs-agent-requirements.lock pyproject.toml
```

将现有锁文件首行中文说明加到解析结果前，再替换 `backend/requirements.lock`。生成后用同一 Python 3.11 临时环境安装锁文件并执行 `pip check`。

在部署清单测试中断言生产锁文件包含 `langfuse==`，避免只改 `pyproject.toml`。

- [ ] **Step 4：验证并提交**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py backend/app/tests/deployment/test_container_manifests.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m pip check`

Expected: `No broken requirements found.`

```bash
git add pyproject.toml backend/requirements.lock backend/app/core/config.py backend/app/tests/unit/test_config.py backend/app/tests/deployment/test_container_manifests.py
git commit -m "build: add langfuse cloud sdk"
```

## Task 3：官方 SDK 生命周期与导出前脱敏

**Files:**
- Create: `backend/app/operations/langfuse_observability.py`
- Create: `backend/app/tests/unit/test_langfuse_observability.py`

- [ ] **Step 1：先写适配层契约测试**

测试只覆盖项目自己的接线和安全边界，不复测 Langfuse 内部队列或重试实现：

- 关闭时不构造 client；
- 启用时将 key、Japan base URL、`app_env`、采样率、独立 provider 和 mask hook 传给 `Langfuse`；
- `shutdown()` 幂等，异常只记录固定事件名；
- 相同业务 trace ID 得到相同的 Langfuse trace ID；
- session key 只以 SHA-256 域隔离摘要上传；
- 代表性的 prompt、completion、message、input、output、reasoning 属性会被删除；
- model、usage、latency、节点名和安全 metadata 保留。

```python
params = MaskOtelSpansParams(
    spans={
        identifier: OtelSpanData(
            trace_id="0" * 32,
            span_id="1" * 16,
            parent_span_id=None,
            name="deepseek-generation",
            instrumentation_scope_name="langchain",
            instrumentation_scope_version=None,
            attributes={
                "langfuse.observation.input": "secret prompt",
                "gen_ai.completion.0.content": "secret answer",
                "gen_ai.usage.input_tokens": 12,
                "gen_ai.response.model": "deepseek-v4-flash",
            },
            resource_attributes={},
        )
    }
)
```

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_observability.py -q`

Expected: FAIL，因为模块尚未实现。

- [ ] **Step 2：实现独立 runtime，直接委托官方 SDK**

核心对象只保存官方 client、独立 `TracerProvider` 和官方 `CallbackHandler`。不实现发送协议、批处理、重试、缓存或价格计算。

```python
provider = TracerProvider(
    resource=Resource.create(
        {
            "service.name": config.service_name,
            "deployment.environment.name": config.app_env,
        }
    )
)
client = Langfuse(
    public_key=config.langfuse_public_key,
    secret_key=config.langfuse_secret_key,
    base_url=config.langfuse_base_url,
    environment=config.app_env,
    sample_rate=config.langfuse_sample_rate,
    tracer_provider=provider,
    mask_otel_spans=mask_langfuse_otel_spans,
)
handler = CallbackHandler()
```

适配层保存单个活动 runtime，供 Graph 和 LLM 入口取得 handler/client。初始化失败属于配置/启动失败；发送失败由官方 SDK 隔离，不能抛回业务请求。

- [ ] **Step 3：实现白名单导出边界**

`mask_langfuse_otel_spans` 使用官方 `MaskOtelSpansResult`/`OtelSpanPatch` 删除最终 span 中的以下数据族：

- `langfuse.observation.input`、`langfuse.observation.output`；
- `gen_ai.prompt.*`、`gen_ai.completion.*`；
- `gen_ai.input.messages`、`gen_ai.output.messages` 及其子字段；
- `input.value`、`output.value`、`mlflow.spanInputs`、`mlflow.spanOutputs`；
- key 中明确表示 reasoning 内容的属性。

不得用宽泛的 `"input" in key` 规则，以免误删 `gen_ai.usage.input_tokens`。mask hook 自身出现异常时必须返回删除所有已知敏感字段的保守 patch，不能让异常导致整批 span 被 Langfuse 丢弃。

- [ ] **Step 4：实现 AgentRun 上下文和 Generation 上下文**

AgentRun 使用业务 trace ID 生成确定性 ID，并只传播安全属性：

```python
langfuse_trace_id = client.create_trace_id(seed=business_trace_id)
with client.start_as_current_observation(
    as_type="agent",
    name="bs-agent.run",
    trace_context={"trace_id": langfuse_trace_id},
    metadata={
        "business_trace_id": business_trace_id,
        "execution_segment": execution_segment,
        "tenant_hash": hash_identifier(tenant_id),
    },
):
    with propagate_attributes(
        session_id=hash_identifier(session_key),
        environment=environment,
    ):
        yield [handler]
```

Generation context 只设置 `name`、`model`、安全的 model parameters 和 usage，不设置 input/output。异常状态只记录稳定错误码或异常类型，不记录原始异常文本。

- [ ] **Step 5：验证并提交**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_observability.py -q`

Expected: PASS。

```bash
git add backend/app/operations/langfuse_observability.py backend/app/tests/unit/test_langfuse_observability.py
git commit -m "feat: add secure langfuse runtime"
```

## Task 4：接入 FastAPI 生命周期，保持业务可用性

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/tests/unit/test_lifecycle.py`
- Modify: `backend/app/tests/unit/test_observability.py`

- [ ] **Step 1：先写启动与关闭失败测试**

覆盖：关闭时不创建 runtime；启用时只创建一次；应用关闭会调用 Langfuse runtime 的幂等 `shutdown()`；Langfuse 运行时关闭失败不会阻止数据库、memory store、object store 等已有资源继续释放；Langfuse 不进入 readiness 依赖列表。

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_lifecycle.py backend/app/tests/unit/test_observability.py -q`

Expected: FAIL，因为 `create_app` 尚未接入 Langfuse runtime。

- [ ] **Step 2：在应用创建时初始化，在 lifespan 末尾关闭**

在现有 `configure_telemetry(...)` 旁调用 `configure_langfuse(...)`，但两者保留独立 runtime。将 Langfuse `shutdown` 放入已有 `close_resources` 队列，不增加 health/readiness 探针。

为测试保留 factory 注入点，避免测试访问 Cloud。日志只能出现固定事件名和异常类型，不能记录 key、URL 查询参数或 payload。

- [ ] **Step 3：验证并提交**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_lifecycle.py backend/app/tests/unit/test_observability.py backend/app/tests/unit/test_langfuse_observability.py -q`

Expected: PASS。

```bash
git add backend/app/main.py backend/app/tests/unit/test_lifecycle.py backend/app/tests/unit/test_observability.py
git commit -m "feat: manage langfuse application lifecycle"
```

## Task 5：将一次 AgentRun 映射为 Langfuse trace

**Files:**
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/tests/integration/test_dynamic_task_graph.py`
- Modify: `backend/app/tests/integration/test_approval_resume.py`

- [ ] **Step 1：先写 Graph 接线失败测试**

用假的 observation context 和假的 callback handler 验证：

- 普通 `invoke` 的 `config["callbacks"]` 含官方 handler；
- `stream` 在整个迭代期间保持 root observation 打开；
- `resume` 复用 checkpoint 中的业务 trace ID，并写 `execution_segment="resume"`；
- 关闭 Langfuse 时 config 不增加 callback，现有返回值不变；
- 观测上下文抛出的 SDK 运行时异常被隔离，Graph 仍正常执行。

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/integration/test_approval_resume.py -q`

Expected: FAIL，因为 Graph 入口尚未建立 Langfuse 上下文。

- [ ] **Step 2：包装三个正式 Graph 入口**

在 `run_question_answer_graph`、`stream_question_answer_graph`、`resume_question_answer_graph` 的实际 `graph.invoke`/`graph.stream` 外围使用 `langfuse_agent_run(...)`。把 context 返回的官方 `CallbackHandler` 合并进 `config["callbacks"]`，不修改 LangGraph 节点实现。

初次执行传入：

```python
execution_segment="initial"
business_trace_id=state["trace_id"]
session_key=state.get("session_key") or session_id
tenant_id=state["tenant_id"]
```

审批恢复从 `snapshot.values` 读取同一组字段，并使用 `execution_segment="resume"`。streaming 的 `with` 必须包住完整 `for graph.stream(...)`，避免生成器尚未消费完 observation 就提前关闭。

- [ ] **Step 3：验证并提交**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/integration/test_approval_resume.py -q`

Expected: PASS。

```bash
git add backend/app/graph/runtime.py backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/integration/test_approval_resume.py
git commit -m "feat: trace agent runs with langfuse"
```

## Task 6：补齐直连 DeepSeek HTTP 的 Generation、Token 与成本输入

**Files:**
- Modify: `backend/app/llm/schemas.py`
- Modify: `backend/app/llm/deepseek.py`
- Modify: `backend/app/tests/unit/test_deepseek_client.py`

- [ ] **Step 1：先写非流式与流式 usage 失败测试**

覆盖：

- 直连 HTTP `complete()` 建立一个 Generation，不传 prompt/output/reasoning；
- OpenAI 兼容 usage 映射为 Langfuse 的 `input`、`output`、`total`；
- streaming 请求带 `stream_options.include_usage=true`；
- `choices=[]` 的最终 usage chunk 能被解析，且不会发给公共 `on_delta`；
- provider 错误只标记固定错误码，业务异常类型和行为保持原样；
- Langfuse 关闭或 SDK observation 失败时，LLM 请求和结果不受影响。

```python
assert generation.updates[-1]["usage_details"] == {
    "input": 12,
    "output": 7,
    "total": 19,
}
assert all("input" not in update for update in generation.updates)
assert all("output" not in update for update in generation.updates)
```

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_deepseek_client.py -q`

Expected: FAIL，因为直连 HTTP 尚未创建 Generation，流式结果也未保留 usage。

- [ ] **Step 2：只为直连 HTTP 增加官方 Generation observation**

`complete_with_streaming_callbacks()` 和 `bind_tools()` 继续交给官方 LangChain callback 自动观测，避免重复 Generation。`complete()` 与 `complete_streamed()` 使用 Task 3 的薄上下文包装直接 HTTP 调用。

启动 Generation 时只写：

```python
{
    "name": "deepseek.chat.completions",
    "model": self._model,
    "model_parameters": {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": bool(thinking),
    },
}
```

完成时只调用 `generation.update(usage_details=...)`。不得调用 `input=...` 或 `output=...`。

- [ ] **Step 3：保留流式 usage**

给 `LlmStreamDelta` 增加内部 `usage` 类型和结构化 usage 字段；解析器先读取顶层 `usage`，再读取 `choices`，从而支持 OpenAI 兼容的空 choices usage chunk。`complete_streamed()` 消费 usage 事件但不调用 `on_delta`，最终把 usage 写回 `LlmCompletionResult` 和 Langfuse Generation。

usage 归一化遵循 Langfuse 官方字段：`prompt_tokens -> input`、`completion_tokens -> output`、`total_tokens -> total`。不上传 provider 返回的未知附加字段，避免 Langfuse 将其误判为扁平、重叠的计费桶。

- [ ] **Step 4：验证并提交**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_deepseek_client.py backend/app/tests/unit/test_llm_invoke.py -q`

Expected: PASS。

```bash
git add backend/app/llm/schemas.py backend/app/llm/deepseek.py backend/app/tests/unit/test_deepseek_client.py
git commit -m "feat: record deepseek generations in langfuse"
```

## Task 7：使用说明与隐私验收文档

**Files:**
- Modify: `README.md`
- Create: `docs/langfuse-cloud-observability.md`
- Modify: `docs/production-deployment.md`

- [ ] **Step 1：写本地最小验证步骤**

文档使用中文，包含：

1. 在 Langfuse Cloud Japan 项目创建 API keys；
2. `cp .env.example .env.local`，填写三项必需变量；
3. `npm run dev:backend`；
4. 从现有前端或 API 发起一次 AgentRun；
5. 在 Langfuse Traces 中按 `business_trace_id` 查找；
6. 查看 agent/node/generation 层级、模型、时延、Token、session；
7. 若成本为空，在 Project Settings > Models 为精确的 DeepSeek model 名称配置价格；
8. 检查所有 observation 的 input/output/reasoning 为空；
9. 关闭 `BS_AGENT_LANGFUSE_ENABLED` 后确认业务仍可运行。

文档明确 `.env.local` 只用于本地；生产环境使用部署平台 secret manager，并且 Cloud Langfuse 不属于 readiness 依赖。

- [ ] **Step 2：记录数据边界和故障语义**

列出允许上传字段和禁止上传字段；说明 PostgreSQL 业务观测表仍是审计事实源，Langfuse 是 LLM/Agent 分析面；说明官方 SDK 负责批处理/重试，业务代码不等待 Cloud 成功响应。

- [ ] **Step 3：检查文档和提交**

Run: `rg -n "prompt|reasoning|回答正文|secret|\.env\.local|Japan|jp\.cloud\.langfuse\.com" README.md docs/langfuse-cloud-observability.md docs/production-deployment.md`

Expected: 能看到明确的禁止上传、密钥管理和 Japan endpoint 说明；不得出现真实 key。

```bash
git add README.md docs/langfuse-cloud-observability.md docs/production-deployment.md
git commit -m "docs: explain langfuse cloud verification"
```

## Task 8：全量验证与 Cloud Japan 手工验收

**Files:**
- Verify only

- [ ] **Step 1：运行静态检查与后端测试**

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests -q`

Expected: PASS。

Run: `npm run test:dev-env`

Expected: PASS。

- [ ] **Step 2：验证依赖与开发入口**

Run: `/opt/anaconda3/bin/python3 -m pip check`

Expected: `No broken requirements found.`

Run: `npm run dev:check -- backend`

Expected: 环境变量齐备且端口预检通过；若本机 backend 端口已被占用，先记录占用进程，不把它误判为 Langfuse 故障。

- [ ] **Step 3：使用用户的 Cloud Japan key 做一条真实 smoke trace**

在未打印 `.env.local`、未打印 key 的前提下启动 backend，发起一条短请求，等待 SDK flush。Cloud UI 必须满足：

- 只有一个以业务 trace ID 确定性关联的根 Agent trace；
- 能看到 LangGraph/LLM 子 observation；
- generation 有精确模型名、时延、input/output/total token；
- session ID 是摘要，不是原始 session key；
- prompt、回答正文、reasoning、工具参数/结果、原始异常均不可见；
- Cloud 暂时不可达时，本地 AgentRun 仍完成，业务 PostgreSQL trace 仍存在。

如果成本仍为空，先在 Langfuse 模型设置中补充与 `deepseek-v4-flash` 精确匹配的定义，再发第二条 trace 验证；不要把价格写进代码。

- [ ] **Step 4：检查最终差异**

Run: `git status --short`

Expected: 只包含本计划范围内的预期文件，且工作区干净。

Run: `git log --oneline --decorate -8`

Expected: 能看到按任务拆分的小提交，设计文档和实施计划仍保留。

## 实现时的官方资料

- [Langfuse Python SDK API](https://python.reference.langfuse.com/langfuse)
- [LangChain/LangGraph integration](https://langfuse.com/integrations/frameworks/langchain)
- [OpenTelemetry coexistence](https://langfuse.com/faq/all/existing-otel-setup)
- [OpenTelemetry export masking](https://langfuse.com/docs/observability/features/masking)
- [Trace IDs and distributed tracing](https://langfuse.com/docs/observability/features/trace-ids-and-distributed-tracing)
- [Token and cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking)
