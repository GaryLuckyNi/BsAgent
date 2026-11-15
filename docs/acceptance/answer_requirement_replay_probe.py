"""固定真实合成输入做边界回放；不执行任何工具，显式 --live 才调用模型。"""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.state import create_initial_state
from app.llm.deepseek import DeepSeekClient
from app.llm.schemas import LlmCompletionResult

FOLDER = ROOT / "data/investigations/2026-10-28-answer-boundary"


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


class Capture:
    async def complete(self, **kwargs):
        self.request = kwargs
        return LlmCompletionResult(content='{"action":"answer"}\n占位回答。', model="capture", provider="capture")


def current_system_prompt(payload):
    state = create_initial_state(session_id="replay", tenant_id="replay", user_id="replay",
                                 user_message=payload["user_message"])
    state["request_flow_version"] = "context-routing-v1"
    state["intent"] = {"intent_type": "financial_research", "goal": payload["goal"]}
    state["plan"] = {"tasks": [{"id": "facts", "task_goal": "复用已捕获材料"}]}
    source = {key: value for key, value in payload["sources"][0].items()
              if key in {"title", "url", "snippet", "published_at"}}
    state["task_results"] = [{"task_id": "facts", "status": "success", "tool_name": "web_search",
                              "content": json.dumps({"query": "回放", "results": [source]}, ensure_ascii=False)}]
    capture = Capture()
    build_synthesizer_node(llm_client=capture)(state)
    system = capture.request["system_prompt"]
    assert "例如（source_1、source_2）" in system, "回放须包含真实来源引用提示"
    return system


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    rows = json.loads((FOLDER / "observations.json").read_text())["data"]
    generation = next(row for row in rows if row["id"] == "7c010b77202bb747")
    original = decode(generation["input"])
    payload = decode(original["user_prompt"])
    system = current_system_prompt(payload)
    cases = [("original_scope", payload)]
    calculation = {**payload, "user_message": "核算当日外资交易对两市成交额的净贡献比例，说明口径。"}
    calculation["goal"] = calculation["user_message"]
    cases.append(("required_calculation", calculation))
    base = {"trace_id": "df0dff78b8193725361d5ea122c8f8ba", "generation_id": generation["id"],
            "system_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "original_input_sha256": hashlib.sha256(original["user_prompt"].encode()).hexdigest(),
            "data_limitations": payload["data_limitations"], "source_count": len(payload["sources"]),
            "scope": "固定材料，只替换合成提示；第二例改为缺少必要计算输入的数值任务。不重新检索或执行工具。"}
    if not args.live:
        print(json.dumps(base, ensure_ascii=False))
        return
    (FOLDER / "replay-system.txt").write_text(system)
    config = Settings(_env_file=str(ROOT / ".env.local"))
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
                            base_url=config.deepseek_base_url, timeout_seconds=90)
    results = []
    try:
        for name, case in cases:
            started = perf_counter()
            user_prompt = original["user_prompt"] if name == "original_scope" else json.dumps(case, ensure_ascii=False)
            result = asyncio.run(client.complete(system_prompt=system, user_prompt=user_prompt,
                                                 temperature=0.2, max_tokens=16_384, thinking=False))
            row = {"case": name, "model": result.model, "thinking": False,
                   "seconds": round(perf_counter() - started, 3), "finish_reason": result.finish_reason,
                   "content": result.content, "usage": result.usage,
                   "input_unchanged": user_prompt == original["user_prompt"]}
            results.append(row)
            (FOLDER / "replay.json").write_text(json.dumps({**base, "runs": results}, ensure_ascii=False, indent=2))
            print(json.dumps({k: v for k, v in row.items() if k not in {"content", "usage"}}, ensure_ascii=False), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
