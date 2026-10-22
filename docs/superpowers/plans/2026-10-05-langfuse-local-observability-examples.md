# Langfuse 4.16.0 本地观测与评估闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 BsAgent 的 Langfuse Cloud EU PoC 完整迁移到本地 Langfuse 4.16.0，并交付内容 Trace、七类 Score、规则与 DeepSeek Evaluator、双 Human Annotation Queue、Prompt、Dataset、Experiment 和真实 Agent/RAG 接入的可复现示例。

**Architecture:** 保留 Langfuse 官方产品源码作为本地参考，由 `langfuse/compose.yaml` 固定并启动 4.16.0 平台；业务侧以统一观测适配层管理内容策略、Trace、Score、Evaluator 和 Annotation。Langfuse 故障与评估故障全部从业务路径隔离，Cloud endpoint 在配置层直接拒绝。

**Tech Stack:** Python 3.11、Langfuse Python SDK 4.16.0、OpenTelemetry、Pydantic 2、httpx、DeepSeek OpenAI-compatible API、LangGraph、Docker Compose、pytest、ruff。

---

## 实施前约束

- 按项目 `AGENTS.md` 约定在当前 feature 分支实施，不新建 worktree。
- 当前工作区已有 Langfuse PoC 改动；每个任务开始前执行 `git status --short` 和目标文件 diff，只暂存本任务文件。
- 不提交 `langfuse-4.16.0/` 上游源码、`.env`、`.env.local`、平台数据、截图或任何 key。
- 不运行 `docker compose down -v`，避免删除本地平台数据。
- 所有正式文档、测试说明和验收报告使用中文。

## 文件结构

### 平台与示例

- `langfuse/compose.yaml`：唯一受支持的本地平台启动入口，固定 Web/Worker 4.16.0 并限制宿主机端口。
- `langfuse/init.py`：幂等生成或补齐本地平台配置，不输出 secret。
- `langfuse/smoke.py`：真实本地 API 验收与脱敏结果输出。
- `langfuse/README.md`：本地平台、功能示例、人工审核和排障说明。
- `scripts/langfuse_examples.py`：Trace、Session/User、Score、Prompt、Dataset、Experiment、Evaluator 和 Annotation 子命令。

### 业务观测模块

- `backend/app/operations/langfuse_content.py`：内容模式、递归脱敏、截断和二进制拒绝。
- `backend/app/operations/langfuse_observability.py`：SDK 生命周期、Trace/Observation 上下文与业务故障隔离。
- `backend/app/operations/langfuse_scores.py`：七个 Score 配置、幂等创建和 Score 写入。
- `backend/app/operations/langfuse_evaluation.py`：规则 Evaluator、DeepSeek judge、结构化结果与平台 Evaluator 配置。
- `backend/app/operations/langfuse_annotation.py`：Annotation Queue Public API 与混合入队策略。
- `backend/app/operations/langfuse_finalize.py`：一次 AgentRun 结束后的根节点更新、评分和人工入队编排。

## Task 1：固定本地 Langfuse 4.16.0 平台

**Files:**
- Modify: `.gitignore`
- Modify: `langfuse/compose.yaml`
- Modify: `langfuse/init.py`
- Modify: `langfuse/.gitignore`
- Modify: `langfuse/README.md`
- Create: `backend/app/tests/deployment/test_langfuse_local_platform.py`

- [ ] **Step 1: 写失败的 Compose 与初始化安全测试**

```python
def test_local_langfuse_compose_pins_416_and_loopback_ports():
    compose = (PROJECT_ROOT / "langfuse" / "compose.yaml").read_text()
    assert "langfuse/langfuse:4.16.0" in compose
    assert "langfuse/langfuse-worker:4.16.0" in compose
    assert "127.0.0.1:3000:3000" in compose
    assert "127.0.0.1:9090:9000" in compose
    assert "cloud.langfuse.com" not in compose


def test_langfuse_init_is_idempotent_and_keeps_secrets_private(tmp_path, capsys):
    env_path = tmp_path / ".env"
    initialize_environment(env_path)
    before = env_path.read_text()
    initialize_environment(env_path)
    assert env_path.read_text() == before
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert "sk-lf-" not in capsys.readouterr().out
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/deployment/test_langfuse_local_platform.py -q`

Expected: FAIL，原因包含镜像未固定为 `4.16.0` 或端口不是本机绑定。

- [ ] **Step 3: 收敛 Compose 与初始化入口**

将 Web/Worker 镜像固定为 `4.16.0`，Web、MinIO、PostgreSQL、ClickHouse、Redis 端口均绑定 `127.0.0.1`。保留健康检查、持久卷、初始化组织/项目/用户字段；设置 `TELEMETRY_ENABLED=false`。`init.py` 只补齐缺失变量，用 `secrets.token_urlsafe` / `secrets.token_hex` 生成值，原子写入并设置 `0o600`。

在根 `.gitignore` 加入：

```gitignore
# 本地下载的 Langfuse 上游源码，仅作为 4.16.0 参考，不纳入 BsAgent 仓库。
langfuse-4.16.0/
```

- [ ] **Step 4: 验证 Compose 与初始化**

Run: `python3 langfuse/init.py`

Expected: 退出码 0，只输出配置文件位置与启动命令，不输出配置值。

Run: `docker compose --project-directory langfuse -f langfuse/compose.yaml config --quiet`

Expected: 退出码 0。

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/deployment/test_langfuse_local_platform.py -q`

Expected: PASS。

- [ ] **Step 5: 提交平台固定版本**

```bash
git add .gitignore langfuse/compose.yaml langfuse/init.py langfuse/.gitignore langfuse/README.md backend/app/tests/deployment/test_langfuse_local_platform.py
git commit -m "build: pin local langfuse 4.16.0"
```

## Task 2：升级 SDK 并建立本地配置契约

**Files:**
- Modify: `pyproject.toml`
- Modify: `backend/requirements.lock`
- Modify: `.env.example`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/tests/unit/test_config.py`
- Modify: `backend/app/tests/deployment/test_container_manifests.py`

- [ ] **Step 1: 写本地 endpoint、内容模式和数值边界测试**

```python
def test_langfuse_defaults_to_local_content():
    config = Settings(_env_file=None)
    assert config.langfuse_base_url == "http://localhost:3000"
    assert config.langfuse_content_mode == "local_content"
    assert config.langfuse_capture_reasoning is False
    assert config.langfuse_max_content_chars == 20_000


@pytest.mark.parametrize(
    "url",
    ["https://cloud.langfuse.com", "https://jp.cloud.langfuse.com"],
)
def test_langfuse_rejects_cloud_hosts(url):
    with pytest.raises(ValidationError, match="本地 Langfuse"):
        Settings(
            _env_file=None,
            langfuse_enabled=True,
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            langfuse_base_url=url,
        )


def test_local_debug_requires_reasoning_switch():
    with pytest.raises(ValidationError, match="capture_reasoning"):
        Settings(_env_file=None, langfuse_content_mode="local_debug")
```

- [ ] **Step 2: 运行配置测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py -q`

Expected: FAIL，默认 URL 和新增字段断言失败。

- [ ] **Step 3: 实现配置字段与 URL 校验**

在 `Settings` 增加并校验：

```python
langfuse_base_url: str = "http://localhost:3000"
langfuse_content_mode: Literal["local_content", "local_debug"] = "local_content"
langfuse_capture_reasoning: bool = False
langfuse_max_content_chars: int = Field(default=20_000, ge=1_000, le=200_000)
langfuse_evaluation_enabled: bool = True
langfuse_deepseek_judge_enabled: bool = True
langfuse_deepseek_judge_timeout_seconds: int = Field(default=30, ge=1, le=120)
langfuse_annotation_enabled: bool = True
langfuse_answer_review_threshold: float = Field(default=0.7, ge=0, le=1)
langfuse_selection_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
langfuse_annotation_sample_rate: float = Field(default=0.05, ge=0, le=1)
langfuse_annotation_retry_attempts: int = Field(default=3, ge=1, le=5)
langfuse_local_allowed_hosts: tuple[str, ...] = (
    "localhost",
    "127.0.0.1",
    "::1",
    "host.docker.internal",
    "langfuse-web",
)
```

启用时要求 hostname 属于 allowlist，scheme 只能是本地 `http` 或 `https`；`local_debug` 与 `capture_reasoning=true` 必须同时满足。

- [ ] **Step 4: 固定 Python SDK 4.16.0 并重建锁文件**

将 `pyproject.toml` 改为 `langfuse==4.16.0`。使用 Python 3.11 临时环境执行：

```bash
/opt/anaconda3/bin/python3 -m venv /private/tmp/bsagent-langfuse-lock
/private/tmp/bsagent-langfuse-lock/bin/python -m pip install pip-tools
/private/tmp/bsagent-langfuse-lock/bin/pip-compile --strip-extras --no-header --output-file /private/tmp/bsagent-requirements.lock pyproject.toml
```

保留锁文件首行中文说明，再替换 `backend/requirements.lock`。不得只手工修改 `langfuse` 一行而跳过依赖解析。

- [ ] **Step 5: 验证配置和依赖**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py backend/app/tests/deployment/test_container_manifests.py -q`

Expected: PASS。

Run: `/private/tmp/bsagent-langfuse-lock/bin/python -m pip install -r backend/requirements.lock && /private/tmp/bsagent-langfuse-lock/bin/python -m pip check`

Expected: `No broken requirements found.`

- [ ] **Step 6: 提交依赖和配置**

```bash
git add pyproject.toml backend/requirements.lock .env.example backend/app/core/config.py backend/app/tests/unit/test_config.py backend/app/tests/deployment/test_container_manifests.py
git commit -m "feat: configure local langfuse runtime"
```

## Task 3：实现本地内容策略

**Files:**
- Create: `backend/app/operations/langfuse_content.py`
- Create: `backend/app/tests/unit/test_langfuse_content.py`

- [ ] **Step 1: 写正文、reasoning、敏感字段和截断测试**

```python
def test_local_content_keeps_business_content_but_drops_reasoning_and_secrets():
    policy = LangfuseContentPolicy(mode="local_content", max_chars=20)
    value = policy.sanitize(
        {
            "prompt": "分析这份材料",
            "answer": "结论来自证据",
            "reasoning": "模型私有推理",
            "authorization": "Bearer secret",
        }
    )
    assert value["prompt"] == "分析这份材料"
    assert value["answer"] == "结论来自证据"
    assert "reasoning" not in value
    assert "authorization" not in value


def test_local_debug_keeps_reasoning_only_when_explicitly_enabled():
    policy = LangfuseContentPolicy(
        mode="local_debug",
        max_chars=20_000,
        capture_reasoning=True,
    )
    assert policy.sanitize({"reasoning": "合成推理"}) == {"reasoning": "合成推理"}


def test_content_policy_truncates_and_reports_lengths():
    policy = LangfuseContentPolicy(mode="local_content", max_chars=4)
    assert policy.sanitize_text("abcdef") == {
        "content": "abcd",
        "truncated": True,
        "original_chars": 6,
        "retained_chars": 4,
    }
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_content.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现不可变内容策略**

实现 `LangfuseContentPolicy`，递归处理 `dict/list/tuple/str`；删除大小写不敏感的 `authorization`、`cookie`、`api_key`、`secret`、`password`、`token` 键和 reasoning 键；`local_debug + capture_reasoning` 只豁免 reasoning。bytes、bytearray 和文件对象返回安全描述，不读取正文。所有输入先复制，不能修改业务对象。

- [ ] **Step 4: 运行内容策略测试和 ruff**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_content.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app/operations/langfuse_content.py backend/app/tests/unit/test_langfuse_content.py`

Expected: `All checks passed!`

- [ ] **Step 5: 提交内容策略**

```bash
git add backend/app/operations/langfuse_content.py backend/app/tests/unit/test_langfuse_content.py
git commit -m "feat: add local langfuse content policy"
```

## Task 4：扩展统一 Trace 与 Generation 适配层

**Files:**
- Modify: `backend/app/operations/langfuse_observability.py`
- Modify: `backend/app/tests/unit/test_langfuse_observability.py`
- Modify: `backend/app/llm/deepseek.py`
- Modify: `backend/app/tests/unit/test_deepseek_client.py`

- [ ] **Step 1: 写根节点、Generation 内容和 reasoning 模式测试**

测试要求 `langfuse_agent_run` 返回含 `callbacks`、`trace_id`、`observation_id` 的 handle，将 `business_trace_id` 同时写入 Trace metadata 与稳定 tag，并允许更新根节点 output；Generation 在 `local_content` 写入 prompt/answer 但不写 reasoning，在 `local_debug` 才写 reasoning。

```python
with langfuse_agent_run(
    business_trace_id="trace-1",
    session_key="session-1",
    tenant_id="tenant-1",
    user_id="user-1",
    execution_segment="initial",
    input={"user_message": "分析收入"},
) as run:
    assert run.trace_id
    assert run.callbacks
    run.update(output={"answer": "收入增长"})
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_observability.py backend/app/tests/unit/test_deepseek_client.py -q`

Expected: FAIL，原因是当前 context 只返回 callback tuple 且删除全部 input/output。

- [ ] **Step 3: 实现 handle 与通用 observation context**

新增：

```python
@dataclass
class LangfuseRunHandle:
    callbacks: tuple[Any, ...]
    trace_id: str | None
    observation_id: str | None
    observation: Any | None
    content_policy: LangfuseContentPolicy | None

    def update(self, *, output: Any = None, metadata: Mapping[str, Any] | None = None) -> None:
        if self.observation is None:
            return
        self.observation.update(
            output=self.content_policy.sanitize(output) if self.content_policy else output,
            metadata=dict(metadata or {}),
        )
```

增加 `langfuse_observation(as_type, name, input, metadata)`，供 Tool、Retriever、Skill selection 和 Evaluator 复用。运行时关闭时返回空 handle。保留故障隔离与幂等 shutdown。

- [ ] **Step 4: 将 DeepSeek input/output 接入 Generation**

`langfuse_generation` 接收 system prompt、user prompt 和内容模式；`_record_langfuse_result` 写入 `content`，仅在策略允许时写 `reasoning_content`，继续写 usage、finish reason、TTFT 和成本字段。不得把 API key、请求 headers 或原始异常写入 metadata。

- [ ] **Step 5: 验证观测和 DeepSeek 回归**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_observability.py backend/app/tests/unit/test_deepseek_client.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 Trace 适配层**

```bash
git add backend/app/operations/langfuse_observability.py backend/app/tests/unit/test_langfuse_observability.py backend/app/llm/deepseek.py backend/app/tests/unit/test_deepseek_client.py
git commit -m "feat: trace local langfuse content"
```

## Task 5：接入真实 Agent、Skill/Tool 与 RAG 层级

**Files:**
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/graph/nodes/tool_agent.py`
- Modify: `backend/app/tools/execution_gateway.py`
- Modify: `backend/app/tests/integration/test_dynamic_task_graph.py`
- Modify: `backend/app/tests/unit/test_tool_agent.py`
- Modify: `backend/app/tests/unit/test_mcp_gateway.py`
- Modify: `backend/app/tests/unit/test_rag_search_tool.py`

- [ ] **Step 1: 写 Agent 根输入输出和 Tool/Retriever 层级测试**

测试断言：

```python
assert observations[0]["as_type"] == "agent"
assert observations[0]["input"] == {"user_message": "查询合同"}
assert observations[-1]["output"]["answer"]
assert tool_observation["as_type"] == "tool"
assert rag_observation["as_type"] == "retriever"
assert rag_observation["output"]["results"][0]["content"] == "证据片段"
```

- [ ] **Step 2: 运行目标测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/unit/test_tool_agent.py backend/app/tests/unit/test_rag_search_tool.py -q`

Expected: FAIL，当前根节点无 IO，Tool/RAG 没有显式 observation。

- [ ] **Step 3: 更新 invoke、stream 和 resume 生命周期**

三条入口都向 `langfuse_agent_run` 传入用户消息和 user ID；invoke/resume 在返回前用最终 `AgentState` 更新根 output。stream 保存最后状态，在生成器正常结束时更新 output；中断、异常和客户端提前关闭时只关闭 context，不伪造回答。

- [ ] **Step 4: 在 Tool Gateway 记录统一 Tool/Retriever observation**

在策略校验后、executor 调用前创建 observation：`rag_search` 使用 `retriever`，其他工具使用 `tool`。input 为已脱敏的 request arguments、purpose、skill、审批要求；output 为规范化的 `ToolCallResult`。失败路径也更新 status 与稳定错误码。

- [ ] **Step 5: 记录 Skill/Tool 候选与选择**

在 `_generate_tool_selection` 外层创建 `skill.selection` span，input 包含 task goal 和候选名，output 包含选中工具、fallback reason 和候选来源；不记录隐藏模型推理。

- [ ] **Step 6: 运行真实链路回归**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/unit/test_tool_agent.py backend/app/tests/unit/test_mcp_gateway.py backend/app/tests/unit/test_rag_search_tool.py -q`

Expected: PASS。

- [ ] **Step 7: 提交 Agent/RAG 接线**

```bash
git add backend/app/graph/runtime.py backend/app/graph/nodes/tool_agent.py backend/app/tools/execution_gateway.py backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/unit/test_tool_agent.py backend/app/tests/unit/test_mcp_gateway.py backend/app/tests/unit/test_rag_search_tool.py
git commit -m "feat: trace agent tools and rag content"
```

## Task 6：建立七个稳定 Score 配置

**Files:**
- Create: `backend/app/operations/langfuse_scores.py`
- Create: `backend/app/tests/unit/test_langfuse_scores.py`

- [ ] **Step 1: 写 Score schema、幂等创建和写入测试**

```python
def test_score_specs_are_bounded_and_stable():
    assert set(SCORE_SPECS) == {
        "task_success",
        "answer_quality",
        "groundedness",
        "selection_correctness",
        "citation_coverage",
        "action_policy_compliance",
        "review_outcome",
    }
    assert SCORE_SPECS["review_outcome"].categories == (
        "accept",
        "needs_revision",
        "reject",
    )
```

测试还要覆盖：已有同名同类型配置复用；同名不同类型拒绝；Trace Score 不带 observation ID；selection Score 必须带 observation ID；API 异常不向业务抛出。

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_scores.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现 ScoreSpec 与 ScoreRegistry**

使用 `runtime.client.api.score_configs.get/create` 幂等维护配置。Numeric 范围固定 0–1；Boolean 不传 categories；Categorical `review_outcome` 使用 `ConfigCategory(value=1,label="accept")`、`0.5/needs_revision`、`0/reject`。Score metadata 固定包含 `evaluator`、`evaluator_version`、`rubric_version`、`dataset_version` 和 `run_id`。

- [ ] **Step 4: 实现容错 Score 写入**

封装 `record_langfuse_score(...)`，调用 `client.create_score`，显式传 `data_type`、`config_id`、trace/observation ID 和 environment。失败只记录事件名与异常类型。

- [ ] **Step 5: 验证并提交 Score 模块**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_scores.py -q`

Expected: PASS。

```bash
git add backend/app/operations/langfuse_scores.py backend/app/tests/unit/test_langfuse_scores.py
git commit -m "feat: add stable langfuse scores"
```

## Task 7：实现规则与 DeepSeek Evaluator

**Files:**
- Create: `backend/app/operations/langfuse_evaluation.py`
- Create: `backend/app/tests/unit/test_langfuse_evaluation.py`

- [ ] **Step 1: 写规则 Evaluator 测试**

覆盖：成功 Run 得到 `task_success=true`；引用覆盖率按已引用 claim 数计算；高风险工具未经审批得到 `action_policy_compliance=false`；没有 Golden expectation 时不伪造 `selection_correctness`。

```python
result = evaluate_rules(
    RuleEvaluationInput(
        completed=True,
        claims_total=2,
        claims_with_citations=1,
        high_risk_actions=1,
        approved_high_risk_actions=1,
        selected_tool="rag_search",
        expected_tools=("rag_search",),
    )
)
assert result.scores["task_success"] is True
assert result.scores["citation_coverage"] == 0.5
assert result.scores["selection_correctness"] is True
```

- [ ] **Step 2: 写 DeepSeek judge schema 和失败降级测试**

`DeepSeekJudgeResult` 只接受 0–1 的 `answer_quality`、`groundedness`，以及布尔 `action_policy_compliance` 和非空理由。无效 JSON、超时和空回答返回 `JudgeFailure(code=...)`，不能生成低分。

- [ ] **Step 3: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_evaluation.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 4: 实现纯规则和 DeepSeekJudge**

规则函数不读取全局设置。`DeepSeekJudge` 使用注入的 `DeepSeekClient`，thinking 关闭，temperature 0，要求单个 JSON 对象；prompt 明确只评估输入、回答、证据、选择和审批事实。解析后写入 judge 模型、rubric `bsagent-judge-v1`、Token 与成本 metadata。

- [ ] **Step 5: 配置 Langfuse 原生 LLM Evaluator 示例**

实现 `ensure_native_answer_evaluator(client, deepseek_key, base_url, model)`：

1. `client.api.llm_connections.upsert` 使用 `LlmAdapter.OPENAI`、DeepSeek base URL、自定义模型；
2. 查找同名 `bsagent-answer-quality-v1` evaluator，存在则复用；
3. 不存在则创建 `CreateEvaluatorRequest_LlmAsJudge`，变量为 `{{input}}`、`{{output}}`，Numeric 输出 0–1；
4. 创建或复用采样率为 0 的 disabled evaluation rule，示例命令显式开启后才运行，避免重复自动评估。

不得打印或返回 DeepSeek key。

- [ ] **Step 6: 验证并提交 Evaluator**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_evaluation.py -q`

Expected: PASS。

```bash
git add backend/app/operations/langfuse_evaluation.py backend/app/tests/unit/test_langfuse_evaluation.py
git commit -m "feat: add langfuse evaluators"
```

## Task 8：实现双 Annotation Queue 与混合入队策略

**Files:**
- Create: `backend/app/operations/langfuse_annotation.py`
- Create: `backend/app/tests/unit/test_langfuse_annotation.py`

- [ ] **Step 1: 写 Public API 和 ReviewPolicy 测试**

使用 `httpx.MockTransport` 断言 Basic Auth 请求以下路径：

```text
GET  /api/public/annotation-queues
POST /api/public/annotation-queues
POST /api/public/annotation-queues/{queue_id}/items
PATCH /api/public/annotation-queues/{queue_id}/items/{item_id}
```

策略测试覆盖低分、规则/judge 不一致、失败、写操作、低置信度和稳定 5% 抽样；同一 `business_trace_id + object_id + reason` 只入队一次。

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_annotation.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现 AnnotationQueueClient**

客户端只接受本地 base URL；使用 `httpx.BasicAuth(public_key, secret_key)`；请求超时 5 秒；对 connect/read timeout 和 5xx 最多重试配置次数，对 4xx 不重试。创建：

- `answer-quality-review`，关联 answer/groundedness/action/review_outcome score config；
- `skill-tool-selection-review`，关联 selection score config。

API 响应只保存 queue/item ID 和状态，不记录认证信息。

- [ ] **Step 4: 实现稳定抽样和幂等键**

```python
def stable_sample(trace_id: str, rate: float) -> bool:
    bucket = int.from_bytes(sha256(trace_id.encode()).digest()[:8], "big")
    return bucket / (2**64 - 1) < rate
```

入队幂等先查询 queue items 并匹配 object ID；已存在时返回现有 item。失败仅写稳定错误事件。

- [ ] **Step 5: 验证并提交 Annotation**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_annotation.py -q`

Expected: PASS。

```bash
git add backend/app/operations/langfuse_annotation.py backend/app/tests/unit/test_langfuse_annotation.py
git commit -m "feat: add langfuse annotation queues"
```

## Task 9：编排 Run 结束后的评估与自动入队

**Files:**
- Create: `backend/app/operations/langfuse_finalize.py`
- Create: `backend/app/tests/unit/test_langfuse_finalize.py`
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/tests/unit/test_lifecycle.py`
- Modify: `backend/app/tests/integration/test_dynamic_task_graph.py`

- [ ] **Step 1: 写 finalizer 顺序与故障隔离测试**

断言顺序为：更新根 output → 规则评分 → 可选 judge → 写 Score → ReviewPolicy → 入队 → flush。Judge 失败时仍写规则 Score；Annotation 失败时仍返回原 AgentState。

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_finalize.py backend/app/tests/unit/test_lifecycle.py -q`

Expected: FAIL，finalizer 尚不存在。

- [ ] **Step 3: 实现 LangfuseRunFinalizer**

`finalize(handle, state, expected=None)` 从 `AgentState` 提取最终回答、claims、引用、tool calls、approval、错误和 selection 置信度。所有外部依赖通过构造器注入；`finalize` 捕获每个阶段异常并继续后续阶段。不得把评估错误写进业务 `AgentState`。

- [ ] **Step 4: 接入 invoke、stream、resume**

invoke/resume 在正常获得最终状态后调用一次；stream 在最后一个状态已发送给客户端后调用一次。使用 `finalized` 标志避免生成器 finally 重复执行。应用 shutdown 仍执行 SDK flush/shutdown。

- [ ] **Step 5: 验证端到端编排**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_finalize.py backend/app/tests/unit/test_lifecycle.py backend/app/tests/integration/test_dynamic_task_graph.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 finalizer**

```bash
git add backend/app/operations/langfuse_finalize.py backend/app/tests/unit/test_langfuse_finalize.py backend/app/graph/runtime.py backend/app/main.py backend/app/tests/unit/test_lifecycle.py backend/app/tests/integration/test_dynamic_task_graph.py
git commit -m "feat: finalize langfuse evaluation loop"
```

## Task 10：实现 Trace、Session/User、Score、Prompt 与 Evaluator 示例

**Files:**
- Create: `scripts/langfuse_examples.py`
- Create: `backend/app/tests/unit/test_langfuse_examples.py`

- [ ] **Step 1: 写 CLI 参数和无网络单元测试**

CLI 子命令固定为：

```text
trace
score
prompt
evaluator
annotation
dataset
experiment
all
```

测试注入 fake client，断言每个子命令只调用对应入口；输出只能包含对象 ID、名称、本地 URL 和通过项，不包含 public/secret key 或 DeepSeek key。

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_examples.py -q`

Expected: FAIL，脚本不存在。

- [ ] **Step 3: 实现 trace、session/user 与三类 Score 示例**

Trace 示例创建 `agent → retriever/tool → generation`，写合成 prompt、证据、工具结果和回答；设置摘要化 user/session。Score 示例写 Numeric `answer_quality`、Boolean `task_success` 和 Categorical `review_outcome`，使用 Task 6 的 config ID。

- [ ] **Step 4: 实现 Prompt 示例**

创建或复用 `bsagent-local-answer-v1` text prompt：

```text
根据以下证据回答问题。
证据：{{context}}
问题：{{question}}
```

通过 `get_prompt(..., label="production")` 获取并 `compile(context=..., question=...)`，在 generation 的 `prompt` 参数关联 Prompt 对象。

- [ ] **Step 5: 实现平台 Evaluator 与 Annotation 示例**

Evaluator 子命令调用 Task 7 的原生 evaluator provisioner；Annotation 子命令创建两个 queue，并把 trace 与 skill selection observation 分别入队。默认只创建 pending item，不伪造人工评分。

- [ ] **Step 6: 验证并提交基础示例**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_examples.py -q`

Expected: PASS。

```bash
git add scripts/langfuse_examples.py backend/app/tests/unit/test_langfuse_examples.py
git commit -m "feat: add langfuse feature examples"
```

## Task 11：实现 Dataset 与 Experiment 示例

**Files:**
- Modify: `scripts/langfuse_examples.py`
- Modify: `backend/app/tests/unit/test_langfuse_examples.py`
- Create: `langfuse/dataset-items.json`

- [ ] **Step 1: 写三条合成 Dataset schema 测试**

数据集固定包含 `readonly_answer`、`rag_answer`、`write_action`，每项必须有 input、expected_output、allowed_tools、expected_citations、risk_level，且不得含真实公司、用户或凭证。

- [ ] **Step 2: 运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_examples.py -q`

Expected: FAIL，dataset 文件和 experiment 入口尚未实现。

- [ ] **Step 3: 实现 Dataset 幂等创建**

使用 `create_dataset` 和带稳定 ID 的 `create_dataset_item`；重复运行更新同一三项，不追加重复项。Dataset 名固定 `bsagent-local-golden-v1`。

- [ ] **Step 4: 实现 Experiment**

使用 `dataset.run_experiment`，`max_concurrency=1`。task 返回合成 Agent 输出；规则 evaluator 返回 `Evaluation` 对象；启用 `--with-deepseek` 时增加 DeepSeek judge。run metadata 包含 prompt、graph、skill 和 knowledge snapshot 版本。

- [ ] **Step 5: 验证并提交 Dataset/Experiment**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_examples.py -q`

Expected: PASS。

```bash
git add scripts/langfuse_examples.py backend/app/tests/unit/test_langfuse_examples.py langfuse/dataset-items.json
git commit -m "feat: add langfuse dataset experiment"
```

## Task 12：改造本地 smoke 并完成 Cloud EU 迁移

**Files:**
- Modify: `langfuse/smoke.py`
- Modify: `backend/app/tests/unit/test_langfuse_smoke.py`
- Delete: `scripts/langfuse_smoke.py`
- Move: `docs/acceptance/2026-09-26-langfuse-eu-smoke.md` → `docs/acceptance/archive/2026-09-26-langfuse-eu-smoke.md`
- Move: `docs/acceptance/2026-09-26-langfuse-eu-smoke.json` → `docs/acceptance/archive/2026-09-26-langfuse-eu-smoke.json`
- Modify: `README.md`
- Rename: `docs/langfuse-cloud-observability.md` → `docs/langfuse-local-observability.md`
- Modify: `docs/langfuse-local-observability.md`
- Modify: `docs/langfuse-usage-and-optimization.md`
- Modify: `docs/production-deployment.md`

- [ ] **Step 1: 写本地 smoke 参数和安全输出测试**

测试要求无 `--region`、无 Cloud URL，默认 base URL 为 localhost；结果 JSON 包含版本、层级、内容、Score、Evaluator、Queue、Prompt、Dataset、Experiment 断言，不含任何 key 或完整 reasoning。

- [ ] **Step 2: 运行 smoke 单元测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_smoke.py -q`

Expected: FAIL，旧脚本仍包含 region/Cloud 路径或断言不完整。

- [ ] **Step 3: 实现本地真实回读验收**

smoke 依次：健康检查 → 创建带唯一 `business_trace_id` 的合成 trace → flush → API 回读并断言该业务 ID、Agent/Skill/Tool/RAG/Generation 层级和正文 → 写/回读七类 Score → 确认原生 evaluator → 创建/回读两个 pending queue item → 创建/读取 Prompt → Dataset/Experiment。`local_content` 扫描完整 reasoning canary 应无匹配；单独 `--debug-reasoning` 运行使用合成 reasoning 验证可回读。

- [ ] **Step 4: 迁移活动文档与历史验收**

活动文档只使用本地 endpoint、4.16.0 命令和本地 UI 路径。历史 EU 报告顶部增加“历史归档，不代表当前配置”；历史 specs/plans 原文不改。所有交叉链接更新到 `docs/langfuse-local-observability.md`。

- [ ] **Step 5: 验证文档和 Cloud 运行路径已移除**

Run: `rg -n "cloud\.langfuse\.com|jp\.cloud\.langfuse\.com|--region" README.md .env.example backend/app scripts/langfuse_examples.py langfuse docs/langfuse-local-observability.md docs/langfuse-usage-and-optimization.md docs/production-deployment.md`

Expected: 无输出。历史 archive、spec 和 plan 不包含在命令范围内。

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_langfuse_smoke.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 smoke 和文档迁移**

```bash
git add README.md .env.example langfuse/smoke.py backend/app/tests/unit/test_langfuse_smoke.py docs/acceptance docs/langfuse-local-observability.md docs/langfuse-usage-and-optimization.md docs/production-deployment.md
git add -u scripts/langfuse_smoke.py docs/langfuse-cloud-observability.md
git commit -m "docs: migrate langfuse workflow to local"
```

## Task 13：运行本地 4.16.0 真实验收

**Files:**
- Create: `docs/acceptance/2026-10-05-langfuse-local-smoke.md`
- Create: `docs/acceptance/2026-10-05-langfuse-local-smoke.json`

- [ ] **Step 1: 启动并确认平台版本**

Run: `python3 langfuse/init.py`

Run: `docker compose --project-directory langfuse -f langfuse/compose.yaml up -d --wait`

Run: `docker compose --project-directory langfuse -f langfuse/compose.yaml ps`

Expected: Web、Worker、PostgreSQL、ClickHouse、Redis、MinIO 全部 running/healthy，Web/Worker image tag 为 `4.16.0`。

- [ ] **Step 2: 运行全部独立示例**

Run: `.venv/bin/python scripts/langfuse_examples.py all`

Expected: 八类示例均返回对象 ID 和本地页面路径，退出码 0，不输出 secret。

- [ ] **Step 3: 运行 local_content smoke**

Run: `.venv/bin/python langfuse/smoke.py`

Expected: `business_trace_id`、层级、正文、七个 Score、两个 Evaluator、两个 pending queue item、Prompt、Dataset、Experiment 全部 PASS；完整 reasoning canary 未找到。

- [ ] **Step 4: 运行 local_debug reasoning smoke**

Run: `.venv/bin/python langfuse/smoke.py --debug-reasoning`

Expected: 仅合成 debug run 可回读 reasoning canary；常规 run 仍无完整 reasoning。

- [ ] **Step 5: 验证 Human Annotation UI**

在 `http://localhost:3000` 打开 `answer-quality-review`，为合成 Trace 填写四个 Score 和备注并完成；打开 `skill-tool-selection-review`，为合成 Observation 填写选择正确性和备注并完成。重新运行：

Run: `.venv/bin/python langfuse/smoke.py --verify-human-annotation`

Expected: 两个指定 item 均为 `COMPLETED`，人工 Score source 与备注存在。

- [ ] **Step 6: 写脱敏验收证据**

Markdown 记录版本、命令、断言数量、Trace/Queue/Dataset/Experiment 本地路径和限制。JSON 只保存 ID、层级、Score、布尔断言、耗时和成本；脚本在写文件前扫描 public key、secret key、DeepSeek key、reasoning canary 和 Authorization canary，发现匹配即失败。

- [ ] **Step 7: 提交验收证据**

```bash
git add docs/acceptance/2026-10-05-langfuse-local-smoke.md docs/acceptance/2026-10-05-langfuse-local-smoke.json
git commit -m "test: accept local langfuse workflow"
```

## Task 14：完成全量回归与最终审查

**Files:**
- Review: all files changed since `bfb0935`

- [ ] **Step 1: 运行 Python 测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests -q`

Expected: 全部 PASS，无 skipped 的 Langfuse 单元或集成测试。

- [ ] **Step 2: 运行 lint 与格式检查**

Run: `/opt/anaconda3/bin/python3 -m ruff check backend scripts langfuse`

Expected: `All checks passed!`

Run: `/opt/anaconda3/bin/python3 -m ruff format --check backend scripts langfuse`

Expected: 退出码 0。

- [ ] **Step 3: 运行依赖、Compose 和 diff 检查**

Run: `.venv/bin/python -m pip check`

Expected: `No broken requirements found.`

Run: `docker compose --project-directory langfuse -f langfuse/compose.yaml config --quiet`

Expected: 退出码 0。

Run: `git diff --check bfb0935..HEAD`

Expected: 无输出。

- [ ] **Step 4: 审查数据边界和范围**

Run: `rg -n "cloud\.langfuse\.com|jp\.cloud\.langfuse\.com" README.md .env.example backend/app scripts/langfuse_examples.py langfuse docs/langfuse-local-observability.md docs/langfuse-usage-and-optimization.md docs/production-deployment.md`

Expected: 无输出。

人工核对：没有 Alerts/Playground 实现；七个 Score 名称与设计一致；Human Annotation 有 Trace/Observation 两个层级；`local_debug` 具备双开关；生产默认关闭 Langfuse。

- [ ] **Step 5: 提交最终必要修正**

只有前四步发现问题时才创建修正提交；每个修正提交只包含对应问题文件，并重新运行受影响检查。全部通过后输出最终复现命令和本地 UI 路径。
