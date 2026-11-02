# 本轮网页来源总览实施计划

> 执行方式：当前会话内逐项实施与验证；用户已确认设计，继续已有相关 feature 分支，不新建 worktree，不自动提交其他未提交工作。

**目标：** 将已确认的来源入口、桌面抽屉和移动底部面板接入正式回答，展示本轮真实搜索网页及实际引用关系。

**架构：** 从当前 request 独立图状态的 `tool_calls` 收集成功搜索返回的网页，合并合成层来源映射，不向模型注入未采用结果。复用已有 `answer_sources`、最终响应和消息元数据通路。前端按安全规范 URL 去重，通过 Markdown AST 判断真实编号链接引用，使用 Radix Dialog 处理焦点、遮罩和关闭。

**技术栈：** Python / LangGraph；React / TypeScript / Radix Dialog / CSS；显式声明项目已安装的 `mdast-util-from-markdown`，不新增模型或搜索调用。

## 文件职责

- 新建 `backend/app/graph/planning/search_sources.py`：收集内置搜索回执，排除未执行、失败、其他工具与无效 URL，合并已有编号并保留返回顺序。
- 修改 `backend/app/graph/nodes/synthesizer.py` 与 `backend/app/graph/runtime.py`：公开来源，覆盖无模型、提前结束分支，不改变模型证据及引用映射。
- 修改 `frontend/src/services/research-sources.ts`：来源分组、去重、网站计数和 Markdown AST 引用关联。
- 新建 `frontend/src/components/research/research-sources.tsx`，从 `research-markdown.tsx` 提取共用站点 icon；修改 `research-message-item.tsx`、`styles.css` 接入。
- 补充对应后端单元、前端服务／组件测试及正式组件验收页，保存验收记录。

## 任务 1：完整来源收集

- [x] 添加失败测试：最终采用 A，但回执中还有旧路径 B，公开列表应有 A、B，A 编号不变；重复 URL、非法地址、其他工具与失败结果不增加来源。
- [x] 实现 `collect_search_sources(state, citation_sources)`，支持对象及 checkpoint 字典回执；只读取成功内置 `web_search` 的固定 `results` 结构，保留顺序并补充稳定编号。
- [x] 合成器公开列表使用该收集器，模型 payload 与 `AnswerCitations` 继续使用原 projection；最终响应再次合并，覆盖无模型／提前返回，复用既有持久化。
- [x] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_answer_citations.py backend/app/tests/unit/test_search_sources.py -q`，确认通过。

## 任务 2：去重与真实引用

- [x] 添加失败测试：同 URL 的多个编号合并；任一匹配编号可标记已引用；错配地址、普通链接、图片和代码链接不算引用。
- [x] 实现 `groupResearchSources(sources, content)`，以 AST 的真实 link 节点、数字标签、URL 联合匹配；引用式链接关联 definition，标题／摘要／日期复用已有值。
- [x] 显式声明已安装的 Markdown 解析依赖，运行来源服务、消息恢复与流事件回归。

## 任务 3：正式入口与面板

- [x] 添加失败测试：真实去重计数、打开列表、引用筛选、关键词过滤、空结果、Esc 关闭与焦点返回、零来源隐藏入口。
- [x] 实现 `ResearchSources({ sources, content })`：Radix Dialog、共用 favicon、安全原文链接，打开时清空过滤，关闭保留聊天位置。
- [x] 在助手正文之后、反馈操作之前接入。添加 445px 桌面抽屉、移动底部 83dvh 面板、独立滚动、固定头尾、安全区及减少动态效果样式。
- [x] 运行前端相关测试与构建。

## 任务 4：验收

- [x] 后端单元、会话／持久化／审批恢复集成回归；前端相关测试与 `npm run build`。
- [x] 使用真实组件浏览器验收入口、过滤、关闭、图标与响应式布局并截图，不发送新研究请求。
- [x] `git diff --check` 与限定 Ruff；记录既有警告，完成验收文档与计划勾选，不自动提交或合并。
