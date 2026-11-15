# Langfuse 4.16.0 本地真实 smoke 验收记录

## 结论

本次已完成代码、配置、隐私策略和回归测试验证，但真实 AgentRun 的 Langfuse 回读验收被本机 Docker 镜像网络阻塞，当前状态为 `blocked`，不得视为真实 smoke 通过。

阻塞原因是 Langfuse 及其依赖镜像无法下载：官方 `docker.langfuse.com` 重定向到 Docker Hub 后连接超时，Docker Hub、Google 镜像缓存和备用镜像代理均未能建立稳定连接。本次未输出或写入任何密钥。

## 已验证项

- Python 依赖完整，Langfuse SDK 为 `4.16.0`，LangChain/LangGraph 兼容组合可正常导入。
- 后端全量回归：`1156 passed, 33 skipped`。
- Langfuse Compose 配置通过解析，Web 和 Worker 镜像精确固定为 `4.16.0`。
- 真实模式会运行最小 Agent Graph 和 DeepSeek，再回读 `bs-agent.run`、`business_trace_id`、DeepSeek generation usage/成本以及内容策略。
- `local_content` 允许脱敏和截断后的 prompt、回答、RAG 和工具数据；reasoning 默认不记录，仅 `local_debug` 且显式开启时记录。
- 七类 Score、规则评估、DeepSeek Judge、双人工标注队列、Prompt、Dataset 和 Experiment 均有可重复运行的实现和单元测试。

## 机器可读结果

[本次真实 smoke JSON](./2026-10-05-langfuse-local-real-smoke.json)

```json
{
  "acceptance_mode": "real_agent",
  "blocked": true,
  "blocker": "local_langfuse_service_unavailable",
  "passed": false
}
```

## 网络恢复后复现

```bash
python langfuse/init.py
docker compose --env-file langfuse/.env -f langfuse/compose.yaml up -d --wait
.venv/bin/python langfuse/smoke.py --real-agent \
  --output docs/acceptance/2026-10-05-langfuse-local-real-smoke.json
```

完整通过时，JSON 必须满足 `passed=true`、`blocked` 不存在，且 `checks` 中层级、`business_trace_id`、DeepSeek usage/成本、内容上传和 reasoning/凭证隔离项全部为 `true`。
