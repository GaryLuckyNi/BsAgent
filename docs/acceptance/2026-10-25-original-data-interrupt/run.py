"""原始数据任务来源确认冒烟；独立会话，只读工具，不自动确认或重发。"""

import argparse
import asyncio
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
source = HERE.parent / "2026-10-24-live-regression-50" / "run_cases.py"
spec = importlib.util.spec_from_file_location("source_confirm_collector", source)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
collector.HERE = HERE
collector.BATCH = "original-data-confirm-20261025-a"
collector.CASES = [
    ("原句来源确认", "宁德时代2026年半年报的营业收入和归母净利润是多少？"),
]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("submit", "collect"))
    args = parser.parse_args()
    if args.mode == "collect":
        await collector.collect()
    else:
        await collector.one(1, asyncio.Semaphore(1))


if __name__ == "__main__":
    asyncio.run(main())
