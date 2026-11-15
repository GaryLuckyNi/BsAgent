# 合成层增量检查与展示补验

日期：2026-11-10。用户反馈上一轮修改后合成层仍整段出现。本轮复查确认，先前的模型入口流式测试不足以证明真实合成协议也能流式展示；本报告补齐合成、持久化订阅与前端展示证据。未调用真实模型、RAG或IdP，未部署。

## 原因与修复

| 阻塞点 | 修复 |
| --- | --- |
| 合成回复以`{"action":"answer"}\n`开头，通用输出检查遇到`{`后把整个后缀收集到结束 | 对完整、无附加字段的既有answer控制头先检查、释放，再独立检查正文。支持拆片、空片、正常空白/CRLF；其它JSON及不完整/非法头仍保守处理。控制头不提供身份或执行授权，合成层继续校验并移除它 |
| `SynthesisStream`只在句末或换行时发布，长句持续生成期间没有正文 | 普通长句保留24字符短尾后按稳定边界增量发布，不等待句号；代码、链接、来源编号和数字/单位不从中间切开。保留违规建议与数值检查 |
| 来源编号渲染为`[1](<https://...>)`后，持久化检查又因URL永久缓冲后文 | 完整且凭证检查不改写的URL单元可在其结束边界释放，后续正文重新进入增量检查；含凭证/畸形材料继续保守缓冲。每片保护单元处理次数有上限，不取消原有文本预算 |
| 回答头已收到被误当成正文已发布 | `answer_started`仅在实际发布内容后设置，保留“未发布可修复、已经发布不能透明重试拼接另一份正文”的约束 |

本轮没有修改业务Agent的安全依赖或新增服务；生产改动仅在公共输出检查和合成流适配。前端生产代码无需修改，它原本会逐个消费正式SSE消息并追加正文，本轮增加了真实解析与Markdown展示的验证。

## 验证

- [122文件清单](2026-11-10-synthesis-stream-test-files.txt)、[联合日志](2026-11-10-synthesis-stream-tests.log)、[JUnit](2026-11-10-synthesis-stream-tests.xml)：**2383通过、14跳过、0失败、0错误**，168.05秒。跳过仍为13项未配置隔离PostgreSQL及1项需显式启用真实模型的评估；保留既有插件导入顺序告警。
- [定向日志](2026-11-10-synthesis-incremental-unit-tests.log)、[JUnit](2026-11-10-synthesis-incremental-unit-tests.xml)：**83通过**。覆盖控制头任意切点、无句末长句、公开引用后继续生成、代码边界、金额/单位24种切点、原有凭证拆片与禁止输出对照。
- [真实合成链路测试](../../../backend/app/tests/integration/test_synthesis_live_pipeline.py)：使用真实DeepSeek同步HTTP适配、真实合成节点、生产用NodeRunner封装、LangGraph、`produce_frames`、数据库事件和`subscribe_events`，仅提供方传输使用MockTransport。让提供方暂停在后半段正文发送之前，断言订阅已收到引用之后的正文；放行后再断言完整重放与最终引用渲染一致，无重复、无控制头外泄。这不是只验证模型callback有片段，也不是只等EOF前一刻才检查。
- [前端测试日志](2026-11-10-synthesis-frontend-tests.log)：**102通过**。[新展示测试](../../../frontend/src/hooks/use-research-session.test.tsx)使用真实`ReadableStream`、`subscribeRunEvents`解析器、会话Hook和`ResearchMarkdown`，仅模拟网络Response。第一段和第二段在`final`前分别进入DOM，状态仍为streaming；终态后内容没有重复。
- [前端构建日志](2026-11-10-synthesis-frontend-build.log)：TypeScript及Vite构建通过；保留已有npm配置提示和大chunk提示。相关生产文件/专项Ruff及`git diff --check`通过。
- 修复前红测：[四个实际阻塞点](2026-11-10-synthesis-incremental-red.log)、[代码单元切点](2026-11-10-synthesis-inline-boundary-red.log)、[数字/单位切点](2026-11-10-synthesis-numeric-boundary-red.log)、[控制头空白](2026-11-10-synthesis-envelope-whitespace-red.log)保留。后两类切点缺陷是在增量改造中发现并修复，未通过删除原有检查换取流式。
- 独立只读复核对两个生产文件及URL后续凭证、userinfo、编码参数、非法/重复字段控制头做禁网探针，与现有专项共90项通过，未发现待修P1/P2。统计与上述回归重叠，不相加。

## 边界

正常正文与完整公开引用可以持续展示；未闭合的代码/链接、疑似凭证后缀仍可能保守缓冲。已经展示的安全前文无法在后续失败时撤回。测试没有证明未知业务秘密、任意编码或真实模型多轮注入均能识别，也没有测真实生产网络首字时间。原上游ACL、业务分类及完整红队等TODO继续保留。
