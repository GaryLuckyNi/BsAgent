# Planner 模块化重构设计

## 目标

在不改变 LangGraph 节点名、`AgentState` 契约、计划结构和路由结果的前提下，将过重的 `planner_agent.py` 拆分为可独立测试的规划能力模块。

## 边界

- `planner_agent.py`：仅负责 LangGraph 节点适配、意图分派与依赖注入。
- `planning/equity_research.py`：负责 A 股任务拆解、分析维度和 MCP/RAG 证据请求。
- `planning/tool_action.py`：负责通用工具参数生成、Schema 校验和失败降级。
- `planning/templates.py`：负责图片、Knowledge、Skill 和 Direct Response 的稳定计划模板。
- `planning/json_output.py`：仅负责从 LLM 输出解析 JSON object。

## 约束

- 对外仍从 `app.graph.nodes.planner_agent` 导入节点构建器。
- 新模块不依赖 `planner_agent.py`，防止循环引用。
- 本次不修改 Prompt、工具选择策略、错误文案和运行时行为。
