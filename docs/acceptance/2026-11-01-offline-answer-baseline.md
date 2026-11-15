# 历史回答离线基线验收

## 交付结果

完成 `historical-answer-baseline-2026-11-01-v4`。固定真实样本 50 条全部尝试，48 条通过结构与逐字引文校验，2 条拒收不发布质量分数；原始响应、失败原因、复核和原答案均分开保存。8 条记录的 11 个争议维度排除，不改写原始模型判断，不补成通过。

确认 4 条历史回答内可复算或逻辑检查的问题，覆盖百分比与倍数、累计与单季、不等号方向、会计口径及 PE 变量角色。其证据、反例和优先级见 `docs/evaluation/2026-11-01-historical-answer-baseline.md`。另记录一条历史工具能力与计划所需输入不匹配，并保留既有空结果兜底的回归案例；不声称已在当前版本复现。

## Langfuse 实际回读

| 集合 | 原生实验 ID | 样本数 | 最终重复发布新增样本 |
| --- | --- | ---: | ---: |
| `bsagent-real-development-v1` | `761f56e874a285e3` | 48 | 0 |
| `bsagent-real-holdout-v1` | `a4e6a66e75f78d5b` | 2 | 0 |

两个实验名称均为 `historical-answer-baseline-2026-11-01-v4`。通过新版 Experiment Items API 逐项核对输入、历史答案、来源 ID 和实验版本元数据。评分使用 `offline_baseline_v4.model.*` 命名：171 个可采用的初评维度，加 50 个评审是否有效的标记，共 221 个；通过 Scores v3 的 subject、值与元数据逐项核对，最终重复发布新增评分 0。

实验 task 仅返回封存历史答案，明确标注 `frozen_historical_answer_not_new_generation`；没有执行业务 Agent、真实业务工具或新的答案生成。实验观察独立存在，不覆盖原始业务 trace；元数据保留原始 trace 与根 observation 关联。评分 1/0.5/0 只表示对应维度的通过/部分/失败，不是概率。

## 验证证据

- 相关 40 项单测通过，覆盖数据集构建、证据完整性、评审输入隔离、逐字引文校验、反事实控制例拦截、不可评与错误不计质量零分、争议维度排除、来源错配拦截、版本冲突、JSON 编码回读、原生实验重复发布及远端篡改保护。
- 6 个相关 Python 文件 Ruff 规则和格式检查通过；`git diff --check` 通过。
- 10 个控制例的 12 项规则一致性检查通过。控制例只使用调优集，不属于人工业务校准。
- 6 项具体题设运算经 Decimal 独立复算通过，容差与展示舍入理由单独记录；没有由此宣称整篇答案正确。
- 私有完整材料在 Git 忽略的 `data/evaluation/2026-11-01/historical-answer-baseline-2026-11-01-v4/`，包括 `case-review.md`、原始 `baseline/`、`review-overlay.json`、`agent-audit.json`、`reviewed-report.json`、`calibration-validation.json`、`numeric-audit.json`、`publish-report.json` 和包含逐文件散列的 `manifest.json`。

发布适配时发现当前本机 Langfuse 为 v4 events_only：旧实验读取返回不可用，旧创建接口返回的兼容对象不能作为成功证明。最终使用原生实验 runner 写入及新版 API 回读；处理了返回 IO 为 JSON 字符串、Scores v3 使用 subject 的实际协议。SDK 对传播 metadata 的超长字段发出警告，但正文保存在 IO，最终已逐项核对完整内容；没有将 metadata 警告当成正文截断而跳过验证。

## 边界

本轮没有修改生产提示词或链路。样本按覆盖类型筛选且跨历史版本；26 条拥有完整合成输入，其中 2 条评审拒收，其他样本不评分事实支持和引用支持。评审模型和生产模型同源，不能替代专家；独立保留样本只有 2 条。下一轮应先做当前版本的固定输入对照，不根据历史初评总数直接宣称提升或调整全链路。
