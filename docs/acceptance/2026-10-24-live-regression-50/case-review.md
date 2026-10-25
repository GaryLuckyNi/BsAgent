# 50 条真实回归逐条审阅表

本表不把 completed 或 research_answer 当作业务通过。审阅范围为本次实际返回与链路证据；不是对所有外部事实的独立审计。

每条观测 JSON 保存原始 Langfuse IO 字符串；review JSON 解码为易读结构。前台生成不含后台 judge；耗时取 bs-agent.run，包含并行耗时但不叠加子 span。

| 编号 | 类别 | 应用回执 | 前台生成 | 根耗时秒 | 审阅结论 | 证据 |
|---|---|---|---:|---:|---|---|
| 001 | 概念 | research_answer | 6 | 22.325 | PE/PB 已回答；无外部数据需求仍有6次前台生成，且把概念任务标为tool；反馈修复1次。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/8202238818a7003071d7e2aa4a34a755) · [审阅IO](review/001.json) · [事件](results/001.json) · [回执](state/001.json) |
| 002 | 概念 | research_answer | 6 | 35.655 | 一般原理已回答；6次前台生成；答案中‘净现比低因折旧摊销大属正常’与前文折旧加回逻辑矛盾。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/6b6e8ef678cb02e2adcb539ffd08e4cd) · [审阅IO](review/002.json) · [事件](results/002.json) · [回执](state/002.json) |
| 003 | 概念 | research_answer | 6 | 35.877 | 量价原理已回答，未查行情；概念链路仍有6次前台生成及整篇分析后重写。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/d7cdbcc76f663494949696f7631dbf89) · [审阅IO](review/003.json) · [事件](results/003.json) · [回执](state/003.json) |
| 004 | 概念 | research_answer | 6 | 39.058 | 框架已回答，未执行外部工具；6次前台生成，回答较长。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/f375bcf98bf88670d1778e41d1b6d757) · [审阅IO](review/004.json) · [事件](results/004.json) · [回执](state/004.json) |
| 005 | 概念 | research_answer | 6 | 31.632 | 杜邦分解已回答，无外部工具；仍有6次前台生成和一次反馈修复。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/794bb8c852d86e2800b6475652296c9f) · [审阅IO](review/005.json) · [事件](results/005.json) · [回执](state/005.json) |
| 006 | 材料计算 | out_of_scope | 1 | 1.551 | 营收/利润同比的材料内计算被误判为out_of_scope，未回答。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/1f124890e839b1f0b2bb8254d3dd7be9) · [审阅IO](review/006.json) · [事件](results/006.json) · [回执](state/006.json) |
| 007 | 材料计算 | research_answer | 6 | 16.829 | PE=20、PB=4计算正确；简单计算仍消耗6次前台生成。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/124e26967f6d06bcc4be77c7f0e0a9d8) · [审阅IO](review/007.json) · [事件](results/007.json) · [回执](state/007.json) |
| 008 | 材料计算 | research_answer | 6 | 11.554 | 自由现金流3亿元、净负债5亿元算对；简单计算仍有6次前台生成。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/6f8ee66efbddcfe8fff0ab2a262e4ab5) · [审阅IO](review/008.json) · [事件](results/008.json) · [回执](state/008.json) |
| 009 | 材料计算 | research_answer | 6 | 15.369 | 把按价格上限20元测得的500万股称为最大数量；题目本身欠定，未澄清上限逻辑。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/4a4e22e6afc3d5d7ee2abce21a1b2b5f) · [审阅IO](review/009.json) · [事件](results/009.json) · [回执](state/009.json) |
| 010 | 材料计算 | research_answer | 6 | 30.547 | 没有仅凭ROE断言乙更好，说明了杠杆和口径限制；仍有6次前台生成。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/014b0c9ac5cd3e1afc6da2aa0a43dca4) · [审阅IO](review/010.json) · [事件](results/010.json) · [回执](state/010.json) |
| 011 | 历史行情 | research_answer | 5 | 9.342 | 收盘价/成交量与daily一致；多写的盘面解读将1482.20>1480.00错误称为低开。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/a1d6c4d4010ddad5b5812ad3f708231d) · [审阅IO](review/011.json) · [事件](results/011.json) · [回执](state/011.json) |
| 012 | 历史行情 | research_answer | 5 | 11.215 | 五日收盘序列与工具一致；附加成交额将约224.4亿元写为2244亿元，且声称前四日涨跌均在±2%内。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/fd2e54f0c07dc77d8a57b88723eae2e1) · [审阅IO](review/012.json) · [事件](results/012.json) · [回执](state/012.json) |
| 013 | 历史行情 | research_answer | 5 | 10.217 | 收盘价与daily一致；amount千元被写成元（差1000倍），并把17.80>17.78称为低开。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/3cdfd403f4bacb0a572be5d1e03c71e6) · [审阅IO](review/013.json) · [事件](results/013.json) · [回执](state/013.json) |
| 014 | 历史行情 | execution_error_response | 6 | 11.182 | 历史指数查到实时rt_idx_k；权限拒绝未被识别，unknown阻塞后续请求，无interrupt。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/f34c12536edaf87e73cf0b28b5716e46) · [审阅IO](review/014.json) · [事件](results/014.json) · [回执](state/014.json) |
| 015 | 历史行情 | research_answer | 5 | 9.913 | 平安银行与上证指数代码区分正确，amount千元及换算一致；日线推断不超出趋势边界。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/72025ecc8841aa2a0404fffb41d86407) · [审阅IO](review/015.json) · [事件](results/015.json) · [回执](state/015.json) |
| 016 | 财务 | research_answer | 5 | 10.616 | 茅台2024年营业收入与归母净利润与income一致，区分营业总收入口径。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/0b5d3561e4a49adadce1a667061ed408) · [审阅IO](review/016.json) · [事件](results/016.json) · [回执](state/016.json) |
| 017 | 财务 | research_answer | 6 | 19.290 | income没有现金流；反馈欲补现金流但复用已完成任务ID，修复失败后停止，目标未完成。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/da8c9569453b5df5a4683fc4f6866a36) · [审阅IO](review/017.json) · [事件](results/017.json) · [回执](state/017.json) |
| 018 | 财务 | research_answer | 7 | 21.912 | 先fina_indicator后income补齐绝对金额，最终数值与报表匹配；存在可避免的首次错误选择。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/ac4f0bf8cfe25801fc31d54042cfa17a) · [审阅IO](review/018.json) · [事件](results/018.json) · [回执](state/018.json) |
| 019 | 财务 | research_answer | 6 | 17.079 | 搜索query自行加入2.00%预设值，最终又纠正用户未提出的2.00%；仅二手转述且可审计正文缺失。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/f995a8bc57556f03d5b9b094da514aae) · [审阅IO](review/019.json) · [事件](results/019.json) · [回执](state/019.json) |
| 020 | 财务 | research_answer | 5 | 15.536 | 列出主营业务，提示分类重叠但仍先加总及计算占比；总收入算术合计不正确。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/ef485d0a0e8f41382e56daf137fac174) · [审阅IO](review/020.json) · [事件](results/020.json) · [回执](state/020.json) |
| 021 | 新闻公告 | research_answer | 8 | 22.073 | anns_d权限拒绝后重新规划直接web_search，无确认；结果保留原文未核实限制，但遗留unknown。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/7c6fa363bb71358f5db44ec70db6c4f7) · [审阅IO](review/021.json) · [事件](results/021.json) · [回执](state/021.json) |
| 022 | 新闻公告 | execution_error_response | 7 | 12.320 | anns_d权限拒绝后，major_news被unknown拦截；用户只看到未核实执行提示，无interrupt。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/4eef02bf83706ef34671e5fa2984105d) · [审阅IO](review/022.json) · [事件](results/022.json) · [回执](state/022.json) |
| 023 | 新闻公告 | research_answer | 6 | 17.023 | 普通搜索给出公告线索并标注二手来源/非穷举；搜索结果snippet与发布日期均空，正文无法独立核验。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/a6bdc0f2df6b901e9a37cf485cda046d) · [审阅IO](review/023.json) · [事件](results/023.json) · [回执](state/023.json) |
| 024 | 新闻公告 | research_answer | 6 | 16.901 | 否定整月降准的结论强于已审计证据；材料未覆盖官方整月公告，不能用未搜到证明没有。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/6e639a4a2fa8f820fc9167139f1abfe1) · [审阅IO](review/024.json) · [事件](results/024.json) · [回执](state/024.json) |
| 025 | 新闻公告 | research_answer | 5 | 11.305 | trade_cal正确选择上一个已结束交易日2026-10-23，区分周末；未混用今日行情。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/b5cea457df8152633a2aecf4890b84de) · [审阅IO](review/025.json) · [事件](results/025.json) · [回执](state/025.json) |
| 026 | 宽泛研究 | research_answer | 10 | 108.291 | 条件化止跌判断，明确指数点位缺口；误用index_global查A股；长耗时仅记录不追查61秒原因。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/fcedd57e02511fd1b1c46dcb4f5daaf3) · [审阅IO](review/026.json) · [事件](results/026.json) · [回执](state/026.json) |
| 027 | 宽泛研究 | research_answer | 12 | 50.612 | 条件化反弹判断并列缺口；两次用index_global查A股返回空，仍缺主要行情；部分生成IO缺失。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/844e3311b9440654e86299c7ed645adc) · [审阅IO](review/027.json) · [事件](results/027.json) · [回执](state/027.json) |
| 028 | 宽泛研究 | research_answer | 8 | 114.289 | 保留历史/最新/待披露边界与数据冲突；主要依赖搜索汇总，可独立核查的原始正文不足。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/93156eca9b9d010c7d9deac96a5de3d8) · [审阅IO](review/028.json) · [事件](results/028.json) · [回执](state/028.json) |
| 029 | 宽泛研究 | research_answer | 13 | 44.437 | 最终聚焦两个驱动，但13次前台生成、两次反馈修复；原始出处和数据时点仍需独立核实。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/f656346260ba60d8fe8ecf6ae06c4b3d) · [审阅IO](review/029.json) · [事件](results/029.json) · [回执](state/029.json) |
| 030 | 宽泛研究 | research_answer | 6 | 39.240 | 没有将高股息等同低风险；引用一条搜索所得分红事实，但统一口径风险数据缺失。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/637da1e684359d5584408a9ff7d77edb) · [审阅IO](review/030.json) · [事件](results/030.json) · [回执](state/030.json) |
| 031 | 混合问题 | execution_error_response | 7 | 20.058 | 理论分析已生成，却因income_vip权限失败而整条退化为错误提示，丢掉可回答部分。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/2967f21f27d7df5066260906ba127d7d) · [审阅IO](review/031.json) · [事件](results/031.json) · [回执](state/031.json) |
| 032 | 混合问题 | execution_error_response | 7 | 18.571 | 现金流与利润理论已生成，却因income_vip权限失败而不输出可回答部分。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/2cdbd24dddf59cc00cc5f3892d509347) · [审阅IO](review/032.json) · [事件](results/032.json) · [回执](state/032.json) |
| 033 | 混合问题 | research_answer | 7 | 24.514 | 回购概念部分保留，repurchase空结果明确不能证明无公告；没有编造具体公告。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/6acc49b49f1cc07dfd6ace0405094422) · [审阅IO](review/033.json) · [事件](results/033.json) · [回执](state/033.json) |
| 034 | 混合问题 | execution_error_response | 7 | 11.189 | 假设PE=20的分析已生成，但财务工具失败后最终仅有错误提示。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/e0d1e5187254aed268c87e551e313703) · [审阅IO](review/034.json) · [事件](results/034.json) · [回执](state/034.json) |
| 035 | 混合问题 | research_answer | 10 | 36.060 | 保留成交量概念但没取得指数成交量；两次index_global空查询，应先修工具适配而非重复规划。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/dd9bbfdb62dad2a4ec398fc95c46dcfe) · [审阅IO](review/035.json) · [事件](results/035.json) · [回执](state/035.json) |
| 036 | 来源限制 | execution_error_response | 6 | 14.361 | A股日线误选hk_daily；第二次相同错误市场查询触发1次/分钟限流，同为40203但不是权限拒绝。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/c022b993fb9228fc60b22121822b8e8b) · [审阅IO](review/036.json) · [事件](results/036.json) · [回执](state/036.json) |
| 037 | 来源限制 | research_answer | 5 | 12.475 | 遵守TuShare-only，income返回原始营收，未用搜索替换；与016/017结构化数值一致。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/e4c6e9fc164179c6aded845ef0a4a5a9) · [审阅IO](review/037.json) · [事件](results/037.json) · [回执](state/037.json) |
| 038 | 来源限制 | research_answer | 5 | 54.943 | 遵守不联网、不调用外部工具；方法回答却拆成两项analysis，5次前台生成。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/9cbf06d5da791d6ee677b61a3a258a27) · [审阅IO](review/038.json) · [事件](results/038.json) · [回执](state/038.json) |
| 039 | 来源限制 | research_answer | 6 | 14.370 | express空结果被去重视为已成功取证；两次修复后planner_feedback_invalid；仅在正文请求确认。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/c7328f67970cb4cc2338188295fce0c0) · [审阅IO](review/039.json) · [事件](results/039.json) · [回执](state/039.json) |
| 040 | 来源限制 | execution_error_response | 6 | 14.373 | TuShare-only未被搜索替换；权限失败后仍留下unknown，替代原始接口被阻塞，缺口提示不具体。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/adadb9788d31928bfbda7935d879b20b) · [审阅IO](review/040.json) · [事件](results/040.json) · [回执](state/040.json) |
| 041 | 并行任务 | research_answer | 7 | 14.866 | 三项独立daily查询均实际发出并settled；两项本地等待后成功，验证三任务健康路径修复有效。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/bdee4d60749a1b81703b4989d6acc674) · [审阅IO](review/041.json) · [事件](results/041.json) · [回执](state/041.json) |
| 042 | 并行任务 | execution_error_response | 9 | 20.763 | 主营收入已取得、公告权限失败；unknown使同批日线始终未发出，最终也没有展示已取得收入。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/c79a13ae5cf77d90536a5f8d142935d1) · [审阅IO](review/042.json) · [事件](results/042.json) · [回执](state/042.json) |
| 043 | 并行任务 | execution_error_response | 6 | 74.910 | 三公司营收仅规划成单工具任务；只查询茅台express空、再查vip权限失败，其他两主体未取证。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/2d6a5546095dfb7924606fde4c379e77) · [审阅IO](review/043.json) · [事件](results/043.json) · [回执](state/043.json) |
| 044 | 并行任务 | research_answer | 6 | 16.862 | 三指数打包单任务，却只查上证index_dailybasic且日期含连字符，空结果后去重禁止补证，三个值均未回答。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/42e6e76949e4c489b97c3d4bec9f3932) · [审阅IO](review/044.json) · [事件](results/044.json) · [回执](state/044.json) |
| 045 | 并行任务 | research_answer | 10 | 26.682 | 明示切换须确认，仍在anns_d权限失败后直接搜索；修订后合成输入丢掉已取得的利润和行情原始值。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/ee6b6021f90ae1a004abc7f4481f04fb) · [审阅IO](review/045.json) · [事件](results/045.json) · [回执](state/045.json) |
| 046 | Skill边界 | execution_error_response | 7 | 17.384 | 请求company_snapshot未执行该Skill，退为普通tool；本地工具依赖及业务意图配置不匹配，随后权限失败。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/5b221adabe644e282b3520d3bae75883) · [审阅IO](review/046.json) · [事件](results/046.json) · [回执](state/046.json) |
| 047 | Skill边界 | execution_error_response | 6 | 13.290 | 请求financial_report_analysis未执行Skill，且A股宁德时代误选us_fina_indicator。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/5f2ccf53a23eff1ce8ec9dae2240047d) · [审阅IO](review/047.json) · [事件](results/047.json) · [回执](state/047.json) |
| 048 | Skill边界 | execution_error_response | 6 | 16.436 | 请求risk_scan未执行Skill，普通公告工具权限失败后未中断确认。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/edef27f3a0ba9620480f31f0c295f5ef) · [审阅IO](review/048.json) · [事件](results/048.json) · [回执](state/048.json) |
| 049 | Skill边界 | execution_error_response | 5 | 15.778 | 实际进入yzj_reasearch工作流，stock_basic超时后停止；保持不造薪酬持股，但未覆盖成功及权限恢复路径。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/384879ea364336cd02ed413ac2a2be91) · [审阅IO](review/049.json) · [事件](results/049.json) · [回执](state/049.json) |
| 050 | Skill边界 | execution_error_response | 5 | 9.699 | 先核实主体、没有替换公司或编造营收；无可验证搜索来源被表达为执行错误，主体未确认语义不足。 | [Trace](http://localhost:3000/project/bsagent-content-local/traces/b1dc6803e3dfb3671eb98c1ed459034e) · [审阅IO](review/050.json) · [事件](results/050.json) · [回执](state/050.json) |

## 关联 ID

| 编号 | 业务 trace_id | Langfuse trace_id | run_id |
|---|---|---|---|
| 001 | trace_ada91915e2ab41e78c1a15f98e89b9f3 | 8202238818a7003071d7e2aa4a34a755 | run_fc6d92217c8663d92e0e59e10fb24e10d5cdfac786d80b97cb1129f02e6c |
| 002 | trace_cf9387188735468d9750fafbd652fa24 | 6b6e8ef678cb02e2adcb539ffd08e4cd | run_94798de0330d4f3e7637f85e13d28377ae6280d676c5b2c245e336195da3 |
| 003 | trace_c96132898e674276a60fb520afa4a686 | d7cdbcc76f663494949696f7631dbf89 | run_2ae540d2d354e199fe98bd799beb1520bc3f23aad66501cab7e9dc43a55e |
| 004 | trace_5d249fc0e465424389bdd95f4d0c13aa | f375bcf98bf88670d1778e41d1b6d757 | run_273aa10b2c7a50dd0d72dcef19776695cb1715260a2511848aff1071e487 |
| 005 | trace_5b300599d04447abad07bf57ea624b71 | 794bb8c852d86e2800b6475652296c9f | run_b7cc1610659e9f0984ae1ff65692dbde310cb22afc78935248f4b7fe99b5 |
| 006 | trace_ac7d53efe9ad4f818d16a764d6b4fc01 | 1f124890e839b1f0b2bb8254d3dd7be9 | run_b32c73a0e1f65ade1940aab685e3aeea221ad28eaba8639a19ae9c6bacd9 |
| 007 | trace_8b23d018329d4d818da08fb2a20e9604 | 124e26967f6d06bcc4be77c7f0e0a9d8 | run_0e326533ec3f3a24320d8c98109e7ea4bf035e3ea628366ca0a97ff53717 |
| 008 | trace_6b054904e85d4df7b733682749cc2964 | 6f8ee66efbddcfe8fff0ab2a262e4ab5 | run_93c819ffe0069671dcaa18920fb3134b83bf1a0480e20b30feaeeb9439c8 |
| 009 | trace_d10051ff13f5484dbfa209f04a2de4dd | 4a4e22e6afc3d5d7ee2abce21a1b2b5f | run_d725cc1ddd38ac953552c2886b1d3516728ad98c0bdfe446ce25103732a3 |
| 010 | trace_e6ef7a0ae55e4afe93a7f45cbd16805e | 014b0c9ac5cd3e1afc6da2aa0a43dca4 | run_b28a9ff91399e30d3d2d288be80d5fdc14c713fa6f25e1aa765f7a81cb68 |
| 011 | trace_4257b69df4f64ee9be41104be7e380e9 | a1d6c4d4010ddad5b5812ad3f708231d | run_f1ae23f76a6687034817aafe8ad99e4da002d718c2119f1bdaa0ae0f7b78 |
| 012 | trace_5c7adf85c3dd4bf19673c11936450249 | fd2e54f0c07dc77d8a57b88723eae2e1 | run_7492345ff4816586fc74bae24e82b9f09a19bf0ce8bc918f3da738059df2 |
| 013 | trace_1fd1c5e02caa464e9e21d0fbaf843c55 | 3cdfd403f4bacb0a572be5d1e03c71e6 | run_d5cea2ef22623948c798ee4a95e19e891ddf7023e95bd7ddd453ef94eae9 |
| 014 | trace_0cd79b84e0bc4b498424c7e97f64ca64 | f34c12536edaf87e73cf0b28b5716e46 | run_ab0568f0290f215f5530a7016465090ade0b80fd5206d282a9c90b592580 |
| 015 | trace_10a2319000aa45de81d97113237bd49b | 72025ecc8841aa2a0404fffb41d86407 | run_0ae3bb5744ed383de2fa028c0d5de1ff2f583a14f5c71ced3f7204b45c09 |
| 016 | trace_5685b235ba584bf89d9437a7b2a70af2 | 0b5d3561e4a49adadce1a667061ed408 | run_a7e755fb3c5936a86edc942f25ae9fe42837887b6917ce65758ad3d93a0e |
| 017 | trace_aa092543160a478dbd28d07be3b18535 | da8c9569453b5df5a4683fc4f6866a36 | run_eba4d6b95bf9fc79b837afe227e5b29495f8af009ad3feac2057523ee230 |
| 018 | trace_25048aac81514c498bba5058647c719c | ac4f0bf8cfe25801fc31d54042cfa17a | run_4da136e8342840c0eba7c3dea83c07e502e33430ef8f85cb24d306ab0982 |
| 019 | trace_af7606e69cc645ecab16d88fee04d8b8 | f995a8bc57556f03d5b9b094da514aae | run_0da111002de64aa53aa57a5b98dbe170d750a3a00fa10df8acd9eef2af60 |
| 020 | trace_97e7480179e54fbaacc844d3077d9486 | ef485d0a0e8f41382e56daf137fac174 | run_e8b68949d62b597417eca37b6fa81d825d1265885385ab480d36f4c0444a |
| 021 | trace_560f105119a4416db180276f7f7a7266 | 7c6fa363bb71358f5db44ec70db6c4f7 | run_b9f24b8e22bbfc88476c6577b96f84aa22801277aeba16f11fdf265e9c87 |
| 022 | trace_9be517b1b7ed482c953ce2e6049ed8f8 | 4eef02bf83706ef34671e5fa2984105d | run_de77ee98ba4a626fd5460f6b4aa5463c88378c0efc766c3df0f422f2b435 |
| 023 | trace_731ff3f0afae421f8e5abe7cc042b8d1 | a6bdc0f2df6b901e9a37cf485cda046d | run_c9ff59e8e2f02a322ba9ef6fefe5b3620b16cd97cd9d6351843885a6334f |
| 024 | trace_0a5ba696f2134de99092bcdbfec49cbe | 6e639a4a2fa8f820fc9167139f1abfe1 | run_9c4b385855aeff3df4b6d9177b49c0f1f5e6bc0a0edef0088eed6a4394cb |
| 025 | trace_e704d784d5f448d5bf054594579418d7 | b5cea457df8152633a2aecf4890b84de | run_f17aef1686194d96a1828bce5514c5f87b19f7beeef29cb73179c25ca468 |
| 026 | trace_d82836d435374739bd79e7ec78916e15 | fcedd57e02511fd1b1c46dcb4f5daaf3 | run_27d64aaf95d7308cbc659ed79c6d061d24084503e32f31cde8e09fc3ec1e |
| 027 | trace_370d3477ad3142418e6831383a8fd7a5 | 844e3311b9440654e86299c7ed645adc | run_20428ff2cae38369d32f5a0759bf4b3248cfd8ec7a7853cfa2d6db6462c8 |
| 028 | trace_926a2e4b8f544f33a00cc834d54a8b07 | 93156eca9b9d010c7d9deac96a5de3d8 | run_83434ae627b86b6a08acbe068078833d2652de644c3175fa2ab877a529b1 |
| 029 | trace_58cc93c2505841ccae85a6e6791796c4 | f656346260ba60d8fe8ecf6ae06c4b3d | run_538113cd63c71198999d70d61cd8c498e1c19377d236787dccf62e9055f3 |
| 030 | trace_16bec4565e5642d49a553c943cf924f8 | 637da1e684359d5584408a9ff7d77edb | run_5bf237b3fa77da79c8d196e0773a4c7bb541dcb4508c65544d16aaab1a83 |
| 031 | trace_ca8103eb802645c0a8dd5f9a06592cce | 2967f21f27d7df5066260906ba127d7d | run_26f6b185ef76ae9b574f15267c3de553edec7ace220a7813aaca979cc8c3 |
| 032 | trace_11ee9332d6b2463eaf03a0f92d2b9d70 | 2cdbd24dddf59cc00cc5f3892d509347 | run_d284d001932c65dbf8ea87f53db542e67d57f11d9b322946045826641dd0 |
| 033 | trace_473fcf7a4b2843ddb429879d10222757 | 6acc49b49f1cc07dfd6ace0405094422 | run_25a494c3efa61efb9978e3b481748d6fcf3f98ecc37e7e9e061c9297484a |
| 034 | trace_b77491bced4648aa9d52c6a25c91f653 | e0d1e5187254aed268c87e551e313703 | run_0de5630dd89e8cb504efe642e15e1bfd8a7360cdb2f7a435d351c50ad4dc |
| 035 | trace_5731c39862c5447c8f2f070eb00a738f | dd9bbfdb62dad2a4ec398fc95c46dcfe | run_6dd4fa88abd8035ea7f6b524371f74a4b4934d98bd5a6e62f674c7893df1 |
| 036 | trace_aca0af4243da41c0be99a294623591e2 | c022b993fb9228fc60b22121822b8e8b | run_777f7992516e17ba15263e416bed8a651e223d237881468121b3307ffaa5 |
| 037 | trace_3d9046db7d6b489c8577360d684a5ca7 | e4c6e9fc164179c6aded845ef0a4a5a9 | run_0f3a4a68a3ee5667bf57fd810cf871ba7b01c0b4eec9952e3d99d40551a1 |
| 038 | trace_111720b12f04494eba1f40357b98e073 | 9cbf06d5da791d6ee677b61a3a258a27 | run_a6920640bf60e56d2baeff2b8156a5071e5634a3829cfb44b9b83ac66dae |
| 039 | trace_9aec8504f8b14052affc8b8ed640e52d | c7328f67970cb4cc2338188295fce0c0 | run_574595336d297f6e9189f08665db89f3b8ba568fe9acbd496a4b90d1d117 |
| 040 | trace_a6b6d67f3dd8487784c03a411f3b1052 | adadb9788d31928bfbda7935d879b20b | run_ea221f07d2d9a7acd8bf2958268525576120a665a4c619ff051d5c9d1a91 |
| 041 | trace_b0c889949a524a60afdcfab59b5d74bf | bdee4d60749a1b81703b4989d6acc674 | run_374493da5abd71a8e5a42ad153be891f3a755f781a995325413b8ae6ee25 |
| 042 | trace_71595d52e0e0420b91d48267ebf60ce3 | c79a13ae5cf77d90536a5f8d142935d1 | run_d6abfe0307579b01399c160ae0fa4bc236da610025029282b659dd8b585f |
| 043 | trace_950dd84f506847cb876fdbda55065d96 | 2d6a5546095dfb7924606fde4c379e77 | run_5e2970aa39cf95968b6a1820792dbc5b35f3fa3d3684995439aa839b7db0 |
| 044 | trace_db87967140094fdcba3cbcb38ec7c43d | 42e6e76949e4c489b97c3d4bec9f3932 | run_f88cac00dd1445f603f4498a5376f8bdab8529344b8c1cce48ac3a638d68 |
| 045 | trace_df4b69a99c984e6fae3d55f31731c342 | ee6b6021f90ae1a004abc7f4481f04fb | run_223fcda6c6218bceb6035eac3ab2fae79471d7e0d76717fb369b3d72375b |
| 046 | trace_3fb06e7169ca45b4b6f129cabe998639 | 5b221adabe644e282b3520d3bae75883 | run_2d69e1ea1937ad747b8f8ebe2e3a97496d33a124f02091d93baf5a9ca1f9 |
| 047 | trace_20bb2f2c17be48debae644bec305b874 | 5f2ccf53a23eff1ce8ec9dae2240047d | run_15a6e30ec2fdca213b0eb08374281f3259b705b46464423818f7410307cd |
| 048 | trace_e673fcf179274d95bd3702aed02d5621 | edef27f3a0ba9620480f31f0c295f5ef | run_4807173f6a1bdf385a410d73b992a66db992780d0561fdba7ddc72b5a873 |
| 049 | trace_fd5044ada9b64399a679c082409cc0b0 | 384879ea364336cd02ed413ac2a2be91 | run_321f22ba2682bd1110ac0608954614ad6d41083aa821317918ed18dfa823 |
| 050 | trace_83681cb0e7dd451f9629c790175edc74 | b1dc6803e3dfb3671eb98c1ed459034e | run_0048f6ab2f2a4ba53b3392b69f02e28132cdf1a7a40199a2594df7761c6a |
