# 工具检索语义对齐实施计划

## 目标

不依赖第三方 MCP 自定义 metadata，直接利用 Planner 生成的能力型查询以及 MCP `name`、`description`、`inputSchema`，提高子任务对专业工具的 Top-K 召回质量；没有合格专业工具时继续降级到 `web_search`。

## 实施步骤

1. 扩展 `SubTaskModel`，允许 Planner 在同一次规划中为数据获取任务输出 `tool_search_query`。该字段使用可区分工具的业务名词和同义表达，不绑定具体工具；仅在参数能够区分工具时加入参数语义。
2. 将 `tool_search_query` 传给 `ToolCandidateResolver`；缺失时兼容使用 `task_goal`，而网页搜索仍使用完整 `task_goal`。
3. 扩充工具检索文档对 `inputSchema` 的索引范围，并对 BM25、Dense、词项覆盖率进行无模型重排。
4. 将默认初始候选池从 20 增加到 50，避免正确工具在融合前被过早裁剪。
5. 收紧专业工具合格判定，移除“任意正分 BM25 即合格”的规则；使用 Dense 阈值或有效词项覆盖率判断。
6. 增加 Planner、候选解析、混合检索和配置测试，并运行相关测试与检索质量评测。

## 验收场景

- “银之杰近期资讯”对应的能力查询应优先召回支持个股代码的上市公司新闻或公告工具。
- 指数公告工具即使包含“公告”，也应因 `index_code` 与个股查询不匹配而降权。
- 公司基本资料工具不应仅因包含“上市公司”和 `ts_code` 而压过新闻或公告工具。
- 只有弱 BM25 重合、Dense 相似度不足的候选不应触发 `tool_choice=required`。
- Planner 未生成 `tool_search_query` 时保持向后兼容。
