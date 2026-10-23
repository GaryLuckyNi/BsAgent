# LangChain 输入输出观测修复计划

> 执行方式：使用 superpowers:subagent-driven-development 在当前会话执行独立模块与两阶段评审；按项目维护约定直接修改 dev，不创建 worktree。

**目标：** 让 ChatOpenAI Generation 和 Intent Router、Planner 节点的输入输出经脱敏后在 Langfuse 可见，不改变任何模型调用、重试或取消行为。

**架构：** 复用 Langfuse 官方 `mask_otel_spans` / `OtelSpanPatch.set_attributes` 导出接口。在独立的内容转换模块中识别 SDK 自动观测：Generation 保留脱敏后的请求/响应，两个指定节点采用业务字段白名单；现有手动观测及未知节点维持原有过滤。复用同一个 `LangfuseContentPolicy`，不继承 SDK 私有类，不新增重复 Generation，不放行原始状态。

**技术栈：** Python、Langfuse 4.16.0、LangChain、OpenTelemetry、pytest。

## 内容契约

- 同时保留 ChatOpenAI 原有 model、usage、时间与层级；只替换导出副本的 observation input/output。
- Intent Router 输入：用户问题、消息上下文、图片摘要；输出：intent、errors。
- Planner 输入：用户问题、intent、消息上下文、plan、task_results、planner_iteration；输出：plan、planner_decision、planner_stop_reason、planner_iteration、errors。
- 仅识别 Langfuse SDK scope 下的 generation 或指定节点 observation；未知 span、trace 级自动正文、原始错误及 metadata 仍删除。
- JSON 先解析再递归脱敏；嵌套 JSON 字符串也检查，防止模型提示词字符串绕过键名过滤。正文整体按 max_chars 有界截断，超大或不可解析的自动采集结构不放行。
- metadata_only 不采集正文；reasoning 默认删除，仅显式 local_debug + capture_reasoning 时保留；凭据任何模式均清理。
- 转换失败仅删除该内容，不影响业务，也不影响同批其它 span；不修改传入状态或输入 span。

## 执行清单

- [x] 新增 `backend/app/tests/unit/test_langfuse_langchain_content.py`：真实 SDK CallbackHandler + 内存 exporter 验证 ChatOpenAI、两个节点内容当前确实被删；包含敏感字段及嵌套 JSON。
- [x] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_langfuse_langchain_content.py -q`，确认因内容缺失而失败。
- [x] 新增 `backend/app/operations/langfuse_langchain_content.py`，集中完成识别、白名单投影、JSON 解码与策略脱敏；修改 `langfuse_observability.py` 在原删除规则之后合并已脱敏 set_attributes，并绑定配置中的策略。
- [x] 补充 metadata_only、debug reasoning、非法/超大 JSON、未知节点、转换异常和原对象不变回归。
- [x] 运行观测、LLM、节点和 durable 定向回归，再执行后端全量与定向 lint；核查无重复 Generation，无真实网络请求。
- [x] 在 `docs/langfuse-local-observability.md` 说明内容范围、配置及仅对新 trace 生效，保留 README 与 .vscode 用户修改。
- [x] 独立评审后汇报结果；不修改本地凭据，不启动/停止用户服务，不推送远程。

## 验证与评审记录

- 真实 SDK / LangGraph / ChatOpenAI 测试使用内存 exporter 和 MockTransport，确认两次模型请求仍只有两个 Generation，业务请求不被脱敏逻辑改写，父子关系、模型及 usage 保留。
- 观测核心测试最终 67 项通过；节点、会话和 durable 交叉回归 138 项通过；定向 ruff 与 diff 检查通过。
- 规格评审通过。边界回归修复普通方括号和引号文本被误丢弃；质量评审发现正文前缀后的多层 JSON 编码遗漏，补充 1～3 层编码及资源预算测试后修复。
- JSON 单字段输入上限 1,000,000 字符，累计解码预算 2,000,000 字符，递归深度 32，最多 1024 个内联片段；解析失败也消耗预算，超过限制只丢弃对应观测字段。
- 最终冻结实现后执行 `.venv/bin/python -m pytest -q --durations=5`：1498 通过、50 跳过；保留原有 pytest 插件重写和测试 JWT 短密钥警告。质量复审通过，原编码 JSON 脱敏缺口已独立复现确认修复。
- 改动保留在当前 dev 工作区，不提交、不推送；没有迁移数据库，也没有重新启动或停止服务。生效需后端/worker 加载新代码后产生新 Trace。
