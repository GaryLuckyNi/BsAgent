# LLM Gateway 与 DeepSeek 接入

## 职责边界

LLM Gateway 是模型调用边界。Graph 节点不应该直接拼 HTTP 请求调用某个模型厂商，而应该通过统一 client 调用。这样后续可以集中处理：

- provider 配置
- API key 管理
- 超时
- 错误归一化
- token usage
- trace 绑定
- 模型切换
- 测试替身

当前第一版只接入 DeepSeek 的 OpenAI-compatible chat completions 接口。

## 配置项

```text
BS_AGENT_LLM_PROVIDER=deepseek
BS_AGENT_DEEPSEEK_API_KEY=...
BS_AGENT_DEEPSEEK_BASE_URL=https://api.deepseek.com
BS_AGENT_DEEPSEEK_MODEL=deepseek-v4-flash
BS_AGENT_LLM_TIMEOUT_SECONDS=30
```

`BS_AGENT_LLM_PROVIDER` 默认是 `deepseek`。生产运行必须有可用 LLM provider；当规则和业务配置无法确定意图时，Intent Router 会调用 LLM 分类器，不能静默降级为 RAG。

## Live Smoke Test

默认测试不会真实调用 DeepSeek。要测试连通性，设置：

```bash
BS_AGENT_RUN_LIVE_DEEPSEEK_TESTS=1 \
BS_AGENT_DEEPSEEK_API_KEY=你的_key \
python3 -m pytest backend/app/tests/integration/test_live_deepseek_llm.py -v
```

该测试只做最小 `ping -> pong` 检查，用于确认 API key、base URL、模型名、网络和响应解析可用。

## Graph 接入点

DeepSeek 当前接入两个位置：

- Intent Router：规则未命中时，LLM 分类器会结合业务意图配置和可用 tool 列表判断 `intent`、`domain`、`sub_intent`、`candidate_skill`、`candidate_tool`。
- Synthesizer：在已有结构化结果基础上做文本渲染。

Synthesizer 仍然先由确定性逻辑生成 `claims`、`cited_evidence_ids`、`missing_information`、`confidence` 和 `response_policy`，然后只把 `content` 交给 LLM 改写。

LLM 不允许新增事实、不允许改 evidence 绑定，也不参与 Verifier 判定。若 LLM 调用失败，Synthesizer 会回退到确定性文本，并在 `response_policy.llm_rendering` 标记为 `failed`。
