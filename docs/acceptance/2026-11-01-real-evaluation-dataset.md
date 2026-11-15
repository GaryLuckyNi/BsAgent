# 真实业务评测集建立验收

## 真实数据与交付结果

已访问当前本机 Langfuse 项目 `bsagent-content-local`，通过 Observations v2 API 完整分页读取 269 条 `bs-agent.run` 根观察，筛选 50 条不同业务问题，保存相关 1,892 条 observation 的本地快照。

入选记录的起止时间为北京时间 2026-10-25 20:26:45 至 2026-11-01 07:15:23。所有样本均来自已有运行，本轮未执行新的模型或业务工具调用。

| 集合 | 名称 | 回读数量 | 最终重复导入新增数 |
| --- | --- | ---: | ---: |
| 调优集 | `bsagent-real-development-v1` | 48 | 0 |
| 保留集 | `bsagent-real-holdout-v1` | 2 | 0 |

访问入口：

- [真实调优集](http://localhost:3000/project/bsagent-content-local/datasets/cmvxeoybj00eeme07mjidzmab)
- [真实保留集](http://localhost:3000/project/bsagent-content-local/datasets/cmvxguofa00ftme07zvnd1v8l)

原问、合成输入、工具调用与结果、来源观察关联均已保存。逐条验收标准独立整理，旧回答单独作为基线，未当作正确答案；所有样本标记为 AI 整理且待业务复核。6 项题设数值标准有独立公式、期望数值、单位和容差。

## 实际完整性限制

- 26 条可固定输入回放合成层，其中调优集 25 条、保留集 1 条。
- 18 条合成输入历史采集时已截断，6 条没有可用合成输入；这两类不能直接用于固定输入忠实度评分。
- 50 条均保留原问与验收规则，可用于相应业务需求和行为检查，但不能声称全部具备完整证据快照。
- 没有完整 checkpoint、工具契约与执行绑定，不声称可确定性重放全链路。
- 相同会话及相同问题族没有跨集合。独立保留样本仅 2 条，规模不足以判断整体泛化。
- 没有新增自动评分、人工金标或新旧模型质量改善的结论。

筛选审计：入选 50，重复问题运行 30，缺少最终输出 21，探针或非业务执行段 64，其他首批覆盖配额外候选 104。不能将未入选者直接当错误样本。

## 验证

- `pytest backend/app/tests/unit/test_real_eval_dataset.py -q`：15 项通过。覆盖 JSON 编码兼容、调用父子关系、截断检测、敏感内容清理、会话和问题族分组、旧答案与标准分离、来源混入拦截、独立数值标准、幂等导入、冲突保护、分页以及浮点往返精度。
- Ruff 格式检查与规则检查通过。
- 两次成功发布均逐项回读核对输入、标准、元数据和原生来源关联；最后一次两个集合新增数均为 0。
- 整数、文本、字段和顺序严格核对；允许 JSON 数字 `1.0` 与 `1` 的等值表示及浮点末位往返误差，没有放宽业务金额或计算容差来掩盖内容变更。
- `git diff --check` 通过。
- `git check-ignore data/evaluation/2026-11-01/items.json` 确认业务资料不进入版本控制。

工具：`scripts/real_eval_dataset.py`。使用边界见 `docs/real-evaluation-dataset.md`。逐项审阅材料为 `data/evaluation/2026-11-01/review.md`，完整导入结果为同目录 `publish-report.json`。
