"""50 条真实只读回归；固定请求 ID，可断点续采，禁止隐式重发。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from dotenv import dotenv_values
from langfuse import Langfuse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
from app.tools.redaction import sanitize_untrusted_details

HERE = Path(__file__).resolve().parent
BATCH = "live50-20261024-a"
HEADERS = {"X-Tenant-ID": "tenant_1", "X-User-ID": "user_1"}
CASES = [
    ("概念", "解释市盈率和市净率的区别，不查询实时数据。"),
    ("概念", "为什么净利润增长但经营现金流下降？只解释一般原理。"),
    ("概念", "缩量上涨和放量下跌分别可能意味着什么？不要查询行情。"),
    ("概念", "如何区分周期股和成长股的估值方法？给出一般分析框架即可。"),
    ("概念", "ROE 的杜邦分解是什么？用一个假设例子解释，不需要外部信息。"),
    ("材料计算", "仅依据以下假设数据：去年营收100亿元，今年120亿元；去年净利润10亿元，今年9亿元。计算营收和利润同比变化及净利率变化，不调用工具。"),
    ("材料计算", "假设股价20元，每股收益1元，每股净资产5元。计算PE和PB并说明局限，只使用题目数据。"),
    ("材料计算", "以下纯假设公司：经营现金流5亿元，资本开支2亿元，有息负债8亿元，现金3亿元。算自由现金流和净负债，不联网。"),
    ("材料计算", "只分析这段虚构公告：公司拟以自有资金1亿元回购，回购价格上限20元，总股本5亿股。计算理论最大回购股数和占比，指出不能保证实施完成。"),
    ("材料计算", "比较两家虚构公司：甲ROE15%、负债率40%，乙ROE20%、负债率75%。仅凭这些数据能否断言乙更优秀？不要外部查询。"),
    ("历史行情", "查询贵州茅台600519.SH在2025年9月1日的A股收盘价与成交量，列明来源和日期。"),
    ("历史行情", "查询宁德时代300750.SZ在2025年9月1日至9月5日的A股日线收盘价，不要使用美股数据。"),
    ("历史行情", "查询汇金科技300561.SZ在2025年9月1日的A股日行情，空结果请明确说明。"),
    ("历史行情", "查询上证指数000001.SH在2025年9月1日至5日的涨跌情况，注意这是指数而不是平安银行。"),
    ("历史行情", "查询2025年9月1日平安银行000001.SZ的收盘价，明确不要混淆上证指数。"),
    ("财务", "查询贵州茅台2024年年报的营业收入和归母净利润，保留单位并标注来源。"),
    ("财务", "查询宁德时代2024年度经营现金流和归母净利润，说明二者差异，缺少数据不要猜测。"),
    ("财务", "查询比亚迪2024年年报营业收入和归母净利润，并核实报告期。"),
    ("财务", "查询招商银行2024年年报净息差，不要把其他银行或其他年度数据代替。"),
    ("财务", "查询汇金科技2024年度按业务划分的主营收入，原始结果为空就明确列出缺口。"),
    ("新闻公告", "检索贵州茅台在2025年9月1日至9月7日发布的公告，列出标题、发布日期与原文链接，没有则说明。"),
    ("新闻公告", "检索宁德时代2025年9月1日至9月7日的公开重要新闻，区分新闻时间和事件时间。"),
    ("新闻公告", "查询汇金科技300561.SZ在2025年9月1日至9月7日是否有重大公告，仅按可核实证据回答。"),
    ("新闻公告", "查询2025年9月中国人民银行公开的降准相关政策信息，如该月没有则明确说明，不编造日期。"),
    ("新闻公告", "最近一个已结束的A股交易日是哪天？查公开信息后说明，不能把今天周六当作交易日。"),
    ("宽泛研究", "你觉得A股会止跌吗？请结合可查证的近期市场证据，区分事实、假设和条件，不给确定性预测。"),
    ("宽泛研究", "最近A股的反弹是真反转还是技术反弹？用有限的关键证据分析，数据不足明确说。"),
    ("宽泛研究", "现在贵州茅台还值得关注吗？给投研框架和可验证证据，不要把历史数据当成最新。"),
    ("宽泛研究", "怎么看当前新能源车产业链的投资机会？聚焦两个关键驱动并交代证据时点。"),
    ("宽泛研究", "银行股高股息就一定低风险吗？结合一条可核实的行业事实解释，不要推荐具体买卖。"),
    ("混合问题", "先直接解释PE为什么可能失真，再查询贵州茅台2024年归母净利润，分清理论与事实。"),
    ("混合问题", "解释经营现金流和利润的区别，再查询宁德时代2024年这两个指标，只在必要时调用工具。"),
    ("混合问题", "先解释什么是回购，再查询贵州茅台在2025年9月是否发布回购相关公告，缺少证据不能推断没有。"),
    ("混合问题", "只用假设数据计算甲公司100亿市值、5亿净利润的PE，再查比亚迪2024年归母净利润，两个部分分别回答。"),
    ("混合问题", "解释为什么成交量重要，并查询上证指数2025年9月1日的成交量作为例子，不要混淆指数和股票。"),
    ("来源限制", "只使用TuShare查询贵州茅台2025年9月1日的日线，不允许web search或网页数据替代；无权限或无数据就明确告知。"),
    ("来源限制", "只使用TuShare原始财务报表查询宁德时代2024年度营收；不接受网页搜索替代，没有权限请说明。"),
    ("来源限制", "不要联网，不调用工具。只介绍如何分析A股是否止跌的方法，不判断当前实际行情。"),
    ("来源限制", "查询贵州茅台2024年营业收入，公开信息可以作为补充；如果需要切换数据来源请先向我确认。"),
    ("来源限制", "我要TuShare的原始涨停池数据，日期2025年9月1日；不要用新闻摘要代替，访问失败请保留明确缺口。"),
    ("并行任务", "分别查询贵州茅台、宁德时代、比亚迪在2025年9月1日的A股收盘价，三个任务独立，不能互相代替。"),
    ("并行任务", "查询汇金科技300561.SZ：2024年主营业务构成、2025年9月1日日行情、2025年9月1日至7日公告。逐项说明证据和缺口。"),
    ("并行任务", "比较贵州茅台、五粮液、泸州老窖2024年营业收入，只需三个数及单位、年度和来源。"),
    ("并行任务", "分别查询2025年9月1日的上证指数000001.SH、深证成指399001.SZ、创业板指399006.SZ收盘点位，不用海外指数。"),
    ("并行任务", "分别查贵州茅台2024年归母净利润、宁德时代2025年9月1日A股行情、比亚迪2025年9月1日至7日公告。只读查询，允许公开信息补充但切换来源需确认。"),
    ("Skill边界", "请使用公司速览工作流company_snapshot生成贵州茅台的公司基础资料、行情和公告速览，缺失字段不要补造。"),
    ("Skill边界", "请使用financial_report_analysis财报分析工作流分析宁德时代2024年度财务，只依据流程实际取得的数据。"),
    ("Skill边界", "请使用risk_scan工作流扫描比亚迪的公告与风险事件，区分实际风险和没有检索到资料。"),
    ("Skill边界", "请按yzj_reasearch技能分析银之杰管理层薪酬和持股，优先最新年度，缺失显示未披露，不允许自行补造。"),
    ("Skill边界", "查询不存在的A股公司“回归测试不存在公司XYZ987654”的股票代码和2024年营收；先核实主体，找不到就停止，不要替换成别的公司。"),
]
assert len(CASES) == 50


def now():
    return datetime.now(timezone.utc).isoformat()


def safe(data):
    return sanitize_untrusted_details(data, max_depth=30, max_nodes=100000,
        max_collection_items=10000, max_string_length=100000)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe(data), ensure_ascii=False, indent=2), encoding="utf-8")


async def one(number, semaphore, replay=False):
    async with semaphore:
        cid = f"{number:03d}"
        path = HERE / "results" / f"{cid}.json"
        if path.exists() and not replay:
            print(json.dumps({"case":cid,"skip":"already_submitted"}), flush=True)
            return
        category, question = CASES[number - 1]
        identity = f"{BATCH}-{cid}"
        result = dict(case_id=cid, category=category, question=question,
            session_id=identity, request_id=identity, started_at=now(), events=[],
            status="submitting", max_seconds=360)
        if replay:
            previous = json.loads(path.read_text())
            result = {**previous, "events":[], "replay_started_at":now(), "collector_note":"初次采集器未兼容 messages 数组，已用 GET 重采原 run，未重发问题"}
            result.pop("transport_error",None)
        save(path, result)  # 提交之前留痕；续跑绝不隐式重复 POST。
        print(json.dumps({"case":cid,"stage":"start","category":category},ensure_ascii=False),flush=True)
        start = time.monotonic()
        async def consume():
            async with httpx.AsyncClient(timeout=httpx.Timeout(45,read=90),trust_env=False) as client:
                method = "GET" if replay else "POST"
                url = (f"http://127.0.0.1:8000/api/sessions/{identity}/runs/{result['run_id']}/events" if replay
                    else f"http://127.0.0.1:8000/api/sessions/{identity}/messages/protocol-stream")
                kwargs = {} if replay else {"json":{"input":{"messages":[{"type":"human","content":question}],"request_id":identity}}}
                async with client.stream(method,url,headers=HEADERS,**kwargs) as response:
                    result["http_status"] = response.status_code
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            try:
                                event = json.loads(line[5:])
                            except ValueError:
                                event = {"malformed_data":line[5:]}
                            result["events"].append(event)
                            if not isinstance(event,dict):
                                continue
                            if event.get("run_id"):
                                result["run_id"] = event["run_id"]
                            if event.get("trace_id"):
                                result["business_trace_id"] = event["trace_id"]
                            if isinstance(event.get("response"),dict) and event["response"].get("trace_id"):
                                result["business_trace_id"] = event["response"]["trace_id"]
                            if event.get("type") == "run_status":
                                result["status"] = event.get("status")
                            if event.get("type") in {"run_started","run_status","user_input_required","error"}:
                                save(path, result)
                result["messages_http_status"] = (messages := await client.get(
                    f"http://127.0.0.1:8000/api/sessions/{identity}/messages",headers=HEADERS)).status_code
                if messages.is_success:
                    result["messages"] = messages.json()
        try:
            await asyncio.wait_for(consume(),timeout=360)
        except Exception as exc:
            result["transport_error"] = {"type":type(exc).__name__,"message":str(exc)}
        result.update(ended_at=now(), elapsed_seconds=round(time.monotonic()-start,3))
        save(path,result)
        print(json.dumps({"case":cid,"stage":"finished","status":result["status"],"seconds":result["elapsed_seconds"],
            "events":len(result["events"]),"trace":result.get("business_trace_id"),"error":result.get("transport_error")},ensure_ascii=False),flush=True)


async def collect():
    values = {**dotenv_values(ROOT/"langfuse/.env"), **dotenv_values(ROOT/".env.local")}
    auth = httpx.BasicAuth(values.get("BS_AGENT_LANGFUSE_PUBLIC_KEY") or values.get("LANGFUSE_INIT_PROJECT_PUBLIC_KEY"),
        values.get("BS_AGENT_LANGFUSE_SECRET_KEY") or values.get("LANGFUSE_INIT_PROJECT_SECRET_KEY"))
    async with httpx.AsyncClient(timeout=60,trust_env=False,auth=auth) as client:
        for path in sorted((HERE/"results").glob("*.json")):
            result = json.loads(path.read_text())
            trace = result.get("business_trace_id")
            if not trace:
                for event in reversed(result["events"]):
                    if isinstance(event,dict) and event.get("trace_id"):
                        trace = event["trace_id"]
                        break
                    if isinstance(event,dict) and isinstance(event.get("response"),dict) and event["response"].get("trace_id"):
                        trace = event["response"]["trace_id"]
                        break
            if not trace:
                print(json.dumps({"case":result["case_id"],"trace_error":"business_trace_missing"}),flush=True)
                continue
            trace_id = Langfuse.create_trace_id(seed=trace)
            params = {"traceId":trace_id,"fields":"core,basic,io,metadata,model,usage,metrics",
                "expandMetadata":"business_trace_id,synthetic_only", "limit":100,
                "fromStartTime":(datetime.fromisoformat(result["started_at"])-timedelta(minutes=1)).isoformat(),
                "toStartTime":(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}
            rows, cursors = [], set()
            try:
                while True:
                    response = await client.get("http://127.0.0.1:3000/api/public/v2/observations",params=params)
                    response.raise_for_status()
                    body = response.json()
                    rows.extend(body["data"])
                    cursor = body.get("meta",{}).get("cursor")
                    if not cursor:
                        break
                    if cursor in cursors:
                        raise RuntimeError("repeated_observation_cursor")
                    cursors.add(cursor)
                    params["cursor"] = cursor
                save(HERE/"observations"/path.name,dict(case_id=result["case_id"],business_trace_id=trace,
                    langfuse_trace_id=trace_id,collected_at=now(),pages=len(cursors)+1,observations=rows))
                print(json.dumps({"case":result["case_id"],"observations":len(rows),"types":dict(Counter(x.get("type") for x in rows))}),flush=True)
            except Exception as exc:
                print(json.dumps({"case":result["case_id"],"trace_error":type(exc).__name__,"message":str(exc)}),flush=True)


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--from-case",type=int,default=1)
    parser.add_argument("--to-case",type=int,default=50)
    parser.add_argument("--collect",action="store_true")
    parser.add_argument("--replay",action="store_true")
    args=parser.parse_args()
    if args.collect:
        await collect()
    else:
        save(HERE/"manifest.json",dict(batch_id=BATCH,mode="真实服务/模型/工具；只读；不自动确认中断",concurrency=2,
            cases=[dict(case_id=f"{i:03d}",category=c,question=q) for i,(c,q) in enumerate(CASES,1)]))
        semaphore=asyncio.Semaphore(2)
        await asyncio.gather(*(one(i,semaphore,args.replay) for i in range(args.from_case,args.to_case+1)))


if __name__ == "__main__":
    asyncio.run(main())
