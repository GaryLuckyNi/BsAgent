"""旧 run 的执行器快照不可恢复时，保留事实并向模型提供未执行错误。"""

import json

from app.graph.planning.contracts import TaskResultModel

CATALOG_ERROR_MESSAGE = "本次运行的工具目录快照已不可恢复，此次恢复未派发新调用；此前调用的结果和未决记录保持原样。请依据已有资料回答，必要时说明需重新发起请求。"


def expired_catalog_update(state):
    content = json.dumps({"error_code": "tool_catalog_changed", "execution_state": "not_started",
                          "message": CATALOG_ERROR_MESSAGE}, ensure_ascii=False)
    completed = {row["task_id"] for row in state.get("task_results", [])}
    results = [TaskResultModel(task_id=task["id"], status="business_failure", content=content,
                               error_code="tool_catalog_changed").model_dump(mode="python")
               for task in (state.get("plan") or {}).get("tasks", []) if task["id"] not in completed]
    return {
        "pending_user_input": None, "user_input_response": None, "pending_tool_approval": None,
        "task_results": results,
        # 没有原执行器就不能在同一个 run 静默换用新契约。此处仍由合成模型组织回答。
        "planner_stop_reason": "tool_catalog_changed", "replan_stop_reason": "tool_catalog_changed",
        "pending_replan": None, "draft_answer": None,
    }
