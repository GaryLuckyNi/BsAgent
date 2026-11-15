"""生成 50 条逐项审阅表与可复核统计；不访问模型或修改业务状态。"""
import ast
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[2]/"backend"))
from app.tools.mcp_permission import classify_mcp_failure

NOTES=[
    "PE/PB 已回答；无外部数据需求仍有6次前台生成，且把概念任务标为tool；反馈修复1次。",
    "一般原理已回答；6次前台生成；答案中‘净现比低因折旧摊销大属正常’与前文折旧加回逻辑矛盾。",
    "量价原理已回答，未查行情；概念链路仍有6次前台生成及整篇分析后重写。",
    "框架已回答，未执行外部工具；6次前台生成，回答较长。",
    "杜邦分解已回答，无外部工具；仍有6次前台生成和一次反馈修复。",
    "营收/利润同比的材料内计算被误判为out_of_scope，未回答。",
    "PE=20、PB=4计算正确；简单计算仍消耗6次前台生成。",
    "自由现金流3亿元、净负债5亿元算对；简单计算仍有6次前台生成。",
    "把按价格上限20元测得的500万股称为最大数量；题目本身欠定，未澄清上限逻辑。",
    "没有仅凭ROE断言乙更好，说明了杠杆和口径限制；仍有6次前台生成。",
    "收盘价/成交量与daily一致；多写的盘面解读将1482.20>1480.00错误称为低开。",
    "五日收盘序列与工具一致；附加成交额将约224.4亿元写为2244亿元，且声称前四日涨跌均在±2%内。",
    "收盘价与daily一致；amount千元被写成元（差1000倍），并把17.80>17.78称为低开。",
    "历史指数查到实时rt_idx_k；权限拒绝未被识别，unknown阻塞后续请求，无interrupt。",
    "平安银行与上证指数代码区分正确，amount千元及换算一致；日线推断不超出趋势边界。",
    "茅台2024年营业收入与归母净利润与income一致，区分营业总收入口径。",
    "income没有现金流；反馈欲补现金流但复用已完成任务ID，修复失败后停止，目标未完成。",
    "先fina_indicator后income补齐绝对金额，最终数值与报表匹配；存在可避免的首次错误选择。",
    "搜索query自行加入2.00%预设值，最终又纠正用户未提出的2.00%；仅二手转述且可审计正文缺失。",
    "列出主营业务，提示分类重叠但仍先加总及计算占比；总收入算术合计不正确。",
    "anns_d权限拒绝后重新规划直接web_search，无确认；结果保留原文未核实限制，但遗留unknown。",
    "anns_d权限拒绝后，major_news被unknown拦截；用户只看到未核实执行提示，无interrupt。",
    "普通搜索给出公告线索并标注二手来源/非穷举；搜索结果snippet与发布日期均空，正文无法独立核验。",
    "否定整月降准的结论强于已审计证据；材料未覆盖官方整月公告，不能用未搜到证明没有。",
    "trade_cal正确选择上一个已结束交易日2026-10-23，区分周末；未混用今日行情。",
    "条件化止跌判断，明确指数点位缺口；误用index_global查A股；长耗时仅记录不追查61秒原因。",
    "条件化反弹判断并列缺口；两次用index_global查A股返回空，仍缺主要行情；部分生成IO缺失。",
    "保留历史/最新/待披露边界与数据冲突；主要依赖搜索汇总，可独立核查的原始正文不足。",
    "最终聚焦两个驱动，但13次前台生成、两次反馈修复；原始出处和数据时点仍需独立核实。",
    "没有将高股息等同低风险；引用一条搜索所得分红事实，但统一口径风险数据缺失。",
    "理论分析已生成，却因income_vip权限失败而整条退化为错误提示，丢掉可回答部分。",
    "现金流与利润理论已生成，却因income_vip权限失败而不输出可回答部分。",
    "回购概念部分保留，repurchase空结果明确不能证明无公告；没有编造具体公告。",
    "假设PE=20的分析已生成，但财务工具失败后最终仅有错误提示。",
    "保留成交量概念但没取得指数成交量；两次index_global空查询，应先修工具适配而非重复规划。",
    "A股日线误选hk_daily；第二次相同错误市场查询触发1次/分钟限流，同为40203但不是权限拒绝。",
    "遵守TuShare-only，income返回原始营收，未用搜索替换；与016/017结构化数值一致。",
    "遵守不联网、不调用外部工具；方法回答却拆成两项analysis，5次前台生成。",
    "express空结果被去重视为已成功取证；两次修复后planner_feedback_invalid；仅在正文请求确认。",
    "TuShare-only未被搜索替换；权限失败后仍留下unknown，替代原始接口被阻塞，缺口提示不具体。",
    "三项独立daily查询均实际发出并settled；两项本地等待后成功，验证三任务健康路径修复有效。",
    "主营收入已取得、公告权限失败；unknown使同批日线始终未发出，最终也没有展示已取得收入。",
    "三公司营收仅规划成单工具任务；只查询茅台express空、再查vip权限失败，其他两主体未取证。",
    "三指数打包单任务，却只查上证index_dailybasic且日期含连字符，空结果后去重禁止补证，三个值均未回答。",
    "明示切换须确认，仍在anns_d权限失败后直接搜索；修订后合成输入丢掉已取得的利润和行情原始值。",
    "请求company_snapshot未执行该Skill，退为普通tool；本地工具依赖及业务意图配置不匹配，随后权限失败。",
    "请求financial_report_analysis未执行Skill，且A股宁德时代误选us_fina_indicator。",
    "请求risk_scan未执行Skill，普通公告工具权限失败后未中断确认。",
    "实际进入yzj_reasearch工作流，stock_basic超时后停止；保持不造薪酬持股，但未覆盖成功及权限恢复路径。",
    "先核实主体、没有替换公司或编造营收；无可验证搜索来源被表达为执行错误，主体未确认语义不足。",
]
assert len(NOTES)==50


def main():
    summaries=json.loads((HERE/"summary.json").read_text())
    results=[json.loads((HERE/"results"/f"{i:03d}.json").read_text()) for i in range(1,51)]
    states=[json.loads((HERE/"state"/f"{i:03d}.json").read_text()) for i in range(1,51)]
    observations=[json.loads((HERE/"observations"/f"{i:03d}.json").read_text()) for i in range(1,51)]
    assert len(summaries)==50
    assert all(r["status"]=="completed" and not r.get("transport_error") for r in results)
    assert all(s["execution"]["producer_done"] and s["run"]["status"]=="completed" for s in states)
    assert len({r["run_id"] for r in results})==len({r["session_id"] for r in results})==50
    assert len({o["langfuse_trace_id"] for o in observations})==50
    assert all(s["run"]["run_id"]==r["run_id"] and s["run"]["trace_id"]==o["business_trace_id"]
        for s,r,o in zip(states,results,observations))
    rows=[r for o in observations for r in o["observations"]]
    assert all(o["pages"]==1 and o["observations"] for o in observations)
    assert all(len([r for r in o["observations"] if r["name"]=="bs-agent.run" and r["endTime"]])==1 for o in observations)
    generations=[r for r in rows if r["type"]=="GENERATION"]
    # API 包装与内部 TOOL span 不能各算一次远端请求；回执数单独统计。
    receipts=[dict(case_id=s["case_id"],**r) for s in states for r in s["receipts"]]
    latency=sorted(s["root_latency"] for s in summaries)
    classification_replay=[]
    for s in summaries:
        for e in s["error_observations"]:
            if "tushare API 错误 [40203]" not in e["statusMessage"]:
                continue
            blocks=ast.literal_eval(e["statusMessage"])
            classification_replay.append(dict(case_id=s["case_id"],name=e["name"],
                captured_message=blocks[0]["text"],
                classification=classify_mcp_failure({"content":blocks,"structured_content":None},error_context=True,pre_execution_semantics=True)))
    checks=dict(verified_at=datetime.now(timezone.utc).isoformat(),requests=50,unique_runs=50,unique_sessions=50,unique_langfuse_traces=50,
        protocol_completed=50,business_response_types=dict(Counter(s["response_type"] for s in summaries)),
        observations=len(rows),generations=len(generations),foreground_generations=sum(s["foreground_generations"] for s in summaries),
        background_judge_generations=sum(s["judge_generations"] for s in summaries),
        reported_total_tokens=sum(s["token_total"] for s in summaries),reported_foreground_tokens=sum(s["foreground_token_total"] for s in summaries),
        root_latency_seconds=dict(min=latency[0],median=statistics.median(latency),mean=statistics.mean(latency),
            p90=latency[math.ceil(50*.9)-1],p95=latency[math.ceil(50*.95)-1],max=latency[-1],method="nearest_rank"),
        generation_names=dict(Counter(r["name"] for r in generations)),
        generation_missing_io=[dict(case_id=o["case_id"],id=r["id"],name=r["name"],missing_input=r.get("input") is None,
            missing_output=r.get("output") is None) for o in observations for r in o["observations"] if r["type"]=="GENERATION" and (r.get("input") is None or r.get("output") is None)],
        generation_without_cost=sum(not r.get("costDetails") for r in generations),
        receipt_counts=dict(Counter(r["status"] for r in receipts)),
        unknown_receipts=[{k:r[k] for k in ("case_id","receipt_id","tool_name","risk_level","status")} for r in receipts if r["status"]=="unknown"],
        classification_replay=classification_replay,
        protocol_period=dict(start=min(s["timings"]["started_at"] for s in states),end=max(s["timings"]["ended_at"] for s in states)))
    (HERE/"verification.json").write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# 50 条真实回归逐条审阅表","","本表不把 completed 或 research_answer 当作业务通过。审阅范围为本次实际返回与链路证据；不是对所有外部事实的独立审计。",
        "","每条观测 JSON 保存原始 Langfuse IO 字符串；review JSON 解码为易读结构。前台生成不含后台 judge；耗时取 bs-agent.run，包含并行耗时但不叠加子 span。",
        "","| 编号 | 类别 | 应用回执 | 前台生成 | 根耗时秒 | 审阅结论 | 证据 |","|---|---|---|---:|---:|---|---|"]
    for s,note in zip(summaries,NOTES):
        cid=s["case_id"]
        lines.append(f"| {cid} | {s['category']} | {s['response_type']} | {s['foreground_generations']} | {s['root_latency']:.3f} | {note} | [Trace](http://localhost:3000/project/bsagent-content-local/traces/{s['langfuse_trace_id']}) · [审阅IO](review/{cid}.json) · [事件](results/{cid}.json) · [回执](state/{cid}.json) |")
    lines.extend(["","## 关联 ID","","| 编号 | 业务 trace_id | Langfuse trace_id | run_id |","|---|---|---|---|"])
    for s,r in zip(summaries,results):
        lines.append(f"| {s['case_id']} | {s['business_trace_id']} | {s['langfuse_trace_id']} | {r['run_id']} |")
    (HERE/"case-review.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in checks.items() if k not in {"classification_replay","unknown_receipts"}},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
