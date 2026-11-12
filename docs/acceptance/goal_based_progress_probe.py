"""离线检查旧规划字段的删除量及目标/任务展示，不请求模型或工具。"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.graph.planning.contracts import PlanModel, SubTaskModel
from app.graph.public_events import emit_public_progress


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def main():
    rows = decode((ROOT / "data/investigations/2026-10-28-tool-query-coverage/observations.json").read_text())["data"]
    by_id = {row["id"]: row for row in rows}
    old_plan = decode(by_id["382c8b3beac68519"]["output"])["content"]
    intent = decode(by_id["9a78717f438307c9"]["output"])["content"]
    current = PlanModel.model_validate(old_plan)
    assert "public_summary" not in current.model_dump()
    assert "public_summary" not in PlanModel.model_json_schema()["properties"]
    assert current.tasks == [SubTaskModel.model_validate(task) for task in old_plan["tasks"]]
    assert "public_summary" in old_plan, "兼容读取不得改写原始旧计划"

    events = []
    with patch("app.graph.public_events.get_stream_writer", return_value=events.append):
        state = {"intent": intent, "plan": current.model_dump(), "planner_iteration": 0}
        emit_public_progress("intent_router", state, status="completed")
        emit_public_progress("planner_agent", state, status="completed")
    assert [event["kind"] for event in events] == ["goal", "plan"]
    assert len(events[1]["message"].splitlines()) == len(current.tasks)
    assert all(event["kind"] != "summary" for event in events)

    # 只比较删除该字段本身，不把 Pydantic 默认字段展开误计为性能差异。
    without_summary = {key: value for key, value in old_plan.items() if key != "public_summary"}

    def compact(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    record = {
        "trace_id": "e63644e40326c60cd3b2c2ab910d5ad8",
        "old_summary_chars": len(old_plan["public_summary"]),
        "old_plan_serialized_chars": len(compact(old_plan)),
        "plan_without_summary_serialized_chars": len(compact(without_summary)),
        "isolated_output_chars_removed": len(compact(old_plan)) - len(compact(without_summary)),
        "task_count": len(current.tasks), "task_contracts_unchanged": True,
        "old_input_unchanged": True, "new_schema_has_public_summary": False,
        "events": events,
        "measurement_scope": "离线字段删除和展示复用；字符数不是token数，未测量真实端到端耗时，未减少业务模型调用次数。",
    }
    output = Path(__file__).with_name("2026-10-28-goal-based-progress.json")
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in record.items() if key != "events"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
