"""复用真实请求采集器；独立只读冒烟，不重跑原50条。"""
import asyncio
import importlib.util
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
source=HERE.parent/"2026-10-24-live-regression-50"/"run_cases.py"
spec=importlib.util.spec_from_file_location("live50_collector",source)
collector=importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
collector.HERE=HERE
collector.BATCH="fixsmoke-20261025-a"
collector.CASES=[
    ("权限确认","查询贵州茅台2025年9月1日至7日公告的公开信息，请先尝试TuShare anns_d。若账号权限不足，我允许补充公开资料，但必须先弹出确认，得到确认再搜索。"),
    ("部分成果","先只用假设市值100亿元、净利润5亿元计算PE；再查询TuShare income_vip提供的比亚迪2024年归母净利润。第二部分只接受该原始接口，禁止网页替代，无权限就列出缺口，但第一部分仍须回答。"),
    ("单位与市场","只查询2025年9月1日汇金科技300561.SZ的A股收盘价、成交量和成交额，保留单位，不需要额外盘面解释。"),
    ("三项派发","分别查询贵州茅台、宁德时代、比亚迪在2025年9月1日的A股收盘价，三项独立，明确证券代码、日期与来源；不需要扩写研究报告。"),
    ("确认边界复测","查询贵州茅台2025年9月1日至7日公告的公开信息，请先尝试TuShare anns_d。若账号权限不足，我允许补充公开资料，但必须先弹出确认，得到确认再搜索。"),
]
if "--collect" not in sys.argv and "--to-case" not in sys.argv:
    sys.argv.extend(["--to-case","4"])
asyncio.run(collector.main())
