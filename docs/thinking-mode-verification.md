# 思考模式验证与研究过程展示

## 结论

DeepSeek 思考模式的响应包含两个独立字段：`reasoning_content` 是提供方原始推理，`content` 是最终回答。BsAgent 会完整解析二者，但产品界面只展示基于真实计划、工具活动和分析结果生成的“研究过程”，不会公开或持久化原始推理。

参考：[DeepSeek 思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)。

## 三条数据通道

1. **后端诊断通道**：`reasoning_content` 被累积到 `LlmCompletionResult.reasoning_content`，供客户端单元测试和后端问题定位使用。
2. **研究过程通道**：LangGraph 节点发布结构化 `progress` 事件，事件类型为 `status`、`activity` 或 `summary`。
3. **最终答案通道**：只有 synthesizer 的 `content` 通过 `answer_delta` 流式发送到答案区。

服务端不会把 `reasoning_content` 转换为 `reasoning_delta` 或 `thinking_delta`。AG-UI 对旧 `thinking_delta` 的解析仍保留兼容，但新请求不会产生这类事件。

## 研究过程事件

- `status` 表示“正在理解问题”“正在制定研究计划”等当前状态，只更新折叠区标题。
- `activity` 表示真实的工具或 Skill 执行。开始和结束事件使用同一个 ID，前端原位更新为成功或失败。
- `summary` 表示面向用户的计划摘要或研究发现。Planner 通过结构化 `public_summary` 输出计划摘要；analysis 和 skill 只在结果通过公开文本检查后生成摘要。

公开文本会限制长度，并过滤 JSON 控制输出、提示词字段、`reasoning_content`、`tool_name`、`skill_name` 等内部标识。没有安全摘要时，界面退化为简短状态。

## 持久化

流式执行期间，API 只收集 `activity` 和 `summary`，按事件 ID 合并状态，最多保存 32 项，每项文本最多 500 字。完成后写入 assistant message 的 `metadata.research_process`。前端加载历史消息时恢复该字段，因此刷新后研究过程仍然可见。

原始 reasoning 不写入公开 SSE、AG-UI 自定义事件、TraceBuffer 或会话 metadata。

## 自动化验证

回归测试覆盖：

- reasoning 与 content 分包或同包时的解析；
- 流式 completion 对完整 reasoning 的后端聚合；
- 公共 handler 忽略 reasoning，只发送最终答案 token；
- 计划摘要、分析摘要、内部文本过滤及工具失败状态；
- 研究过程事件的稳定排序、原位更新和 metadata 恢复；
- 研究折叠区的用户控制、计时结束和最终答案独立渲染。

页面标题使用“正在研究…”和“研究完成（用时 N 秒）”。这里的时间是答案开始前的研究阶段耗时，包含规划、工具和分析等待，并非 DeepSeek 提供的纯模型推理时长。
