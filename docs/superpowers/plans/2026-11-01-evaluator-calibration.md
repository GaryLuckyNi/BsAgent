# 评审校准与独立计算验收实施计划

目标：先提高评测可用性，再诊断短候选；不改生产，不覆盖旧实验。设计见 `docs/superpowers/specs/2026-11-01-evaluator-calibration-design.md`。本会话按步骤执行并复核，不另行委派或操作已有业务分支。

- [x] 新建 `backend/app/tests/unit/test_numeric_evaluation.py`。先写失败测试，覆盖总和、符号、边界、期间、成本分母、引文失配、未绑定不默认通过；实现 `scripts/numeric_evaluation.py` 的Decimal运算及原句绑定检查。
- [x] 新建 `backend/app/tests/unit/test_evaluator_calibration.py`。先验证标准视图不改变原Dataset、基数直接值允许、纯假设不增核验、证据缺口不算全完成、评测标准不注入生成端、缓存版本变化拒绝覆盖；实现 `scripts/evaluator_calibration.py`。
- [x] 冻结 `docs/evaluation/offline-answer-judge-v2.md` 与合成校准案例。执行已配置模型校准，保留所有初次有效输出及有限格式修复，不按得分重采样。
- [x] 检查关键误判门槛，决定是否进入短候选实验。若校准失败，保存失败记录并停止依赖该裁判的质量对照，不能以改错分或放宽检查冒充通过。
- [x] 候选阶段的条件判定已完成：校准仍有关键评分矛盾，未通过门槛，故本轮不启动24次候选配对生成，不新增候选或宣称其效果。
- [x] 保存结果、测试、来源散列、缓存验证与中文报告。因候选门槛未过，本轮不发布新的真实Langfuse质量实验，历史实验保持封存。

针对性验证：`.venv/bin/python -m pytest backend/app/tests/unit/test_numeric_evaluation.py backend/app/tests/unit/test_evaluator_calibration.py -q`，再运行既有离线评测相关63项测试及Ruff。数值运算只接收明确的操作枚举，不调用eval，不执行业务工具。
