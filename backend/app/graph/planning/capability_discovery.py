"""Planner 的单轮能力检索协议；不授权业务工具执行。"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.tools.redaction import reject_inline_credentials


class CapabilitySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["discover_tools"]
    queries: list[str] = Field(min_length=1, max_length=3)

    @field_validator("queries")
    @classmethod
    def validate_queries(cls, queries):
        values = [" ".join(query.split()) for query in queries]
        if any(not value or len(value) > 512 for value in values) or len(set(values)) != len(values):
            raise ValueError("能力检索词须为1至3条不重复的非空短句，每条最多512字符。")
        reject_inline_credentials(values)
        return values


class FactCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_fact: str = Field(min_length=1, max_length=500)
    status: Literal["covered", "available", "unknown"]
    task_id: str | None = None
    candidate_ref: str | None = None
    output_meaning: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def distinguish_available_capability(cls, value):
        # 有任务与候选绑定时，available 表达可调用能力，不表示数据已经取得。
        if isinstance(value, dict) and value.get("status") == "available" and value.get("task_id") and value.get("candidate_ref"):
            return {**value, "status": "covered"}
        return value


def coverage_search_request(payload):
    """根据 Planner 自己声明的缺口路由；只使用其能力检索词，不复制任务目标。"""
    rows, raw_tasks = payload.get("coverage", []), payload.get("tasks", [])
    if not isinstance(rows, list) or len(rows) > 64 or not isinstance(raw_tasks, list):
        raise ValueError("覆盖映射与任务必须为数组，覆盖映射最多64条。")
    tasks = _tasks_by_id(raw_tasks)
    queries = []
    for raw in rows:
        item = FactCoverage.model_validate(raw)
        if item.status == "unknown":
            query = tasks.get(item.task_id, {}).get("tool_search_query")
            if not query:
                raise ValueError("未知必要能力须提供对应任务的能力检索短句，或显式申请discover_tools。")
            queries.append(query)
    return (CapabilitySearchRequest(action="discover_tools", queries=list(dict.fromkeys(queries)))
            if queries else None)


def _tasks_by_id(raw_tasks):
    tasks = {}
    for task in raw_tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise ValueError("任务必须是对象且id必须为字符串。")  # noqa: TRY004 - 模型结构错误统一进入格式修复。
        refs = task.get("tool_candidate_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("任务候选引用必须为字符串数组。")
        query = task.get("tool_search_query")
        if query is not None and not isinstance(query, str):
            raise ValueError("能力检索词必须为字符串。")
        if task["id"] in tasks:
            raise ValueError("任务id不能重复。")
        tasks[task["id"]] = task
    return tasks


def without_coverage(payload):
    """校验覆盖映射的引用绑定；业务语义仍由 Planner 判断，不成为执行凭据。"""
    if "coverage" not in payload:
        return payload
    rows = payload["coverage"]
    if not isinstance(rows, list) or len(rows) > 64 or not isinstance(payload.get("tasks", []), list):
        raise ValueError("覆盖映射必须是最多64条的数组。")
    tasks = _tasks_by_id(payload.get("tasks", []))
    for raw in rows:
        item = FactCoverage.model_validate(raw)
        if item.status == "available":
            if item.task_id is not None or item.candidate_ref is not None:
                raise ValueError("已有事实不应冒充新的取证任务。")
            continue
        task = tasks.get(item.task_id)
        if task is None:
            raise ValueError("必要事实必须绑定本次计划中的取证任务。")
        if item.candidate_ref is not None and item.candidate_ref not in task.get("tool_candidate_refs", []):
            raise ValueError("覆盖声明中的候选必须属于对应任务。")
        if item.status == "covered" and not task.get("skill_name") and (
                not item.candidate_ref or not item.output_meaning):
            raise ValueError("工具覆盖声明须给出候选和明确的返回含义，未知能力不能声明已覆盖。")
    return {key: value for key, value in payload.items() if key != "coverage"}


def capability_search_prompt(prompt, *, remaining_rounds, attempted_queries=()):
    payload = json.loads(prompt)
    coverage_schema = [{
        "required_fact": "完成用户目标必需的一个事实，保留期间和指标口径",
        "status": "covered=已披露能力能返回；available=上下文已有业务数据无需调用；unknown=能力缺口",
        "task_id": "承接事实的任务ID；已有事实填null",
        "candidate_ref": "对应任务内能直接返回该事实的候选ref；未知或Skill填null",
        "output_meaning": "依据已披露返回说明简述为什么能取得该事实，区分原始量与衍生值；未知填null",
    }]
    payload["required_schema"] = {"coverage": coverage_schema, **{
        key: value for key, value in payload["required_schema"].items() if key != "coverage"}}
    payload["capability_search"] = {
        "remaining_rounds": remaining_rounds,
        "attempted_queries": list(attempted_queries),
        "instruction": ("逐项核对必要事实与单个候选的返回含义。只有明确覆盖时才返回coverage和tasks；"
                        "缺少或未知的必要能力应先请求一次discover_tools。不得把互补候选拼进一个任务假装完成覆盖，"
                        "也不能将比率、增长率等衍生值当作原始输入。仅描述缺口的业务能力，不执行业务工具。"
                        if remaining_rounds else
                        "能力补检索已结束，只能返回最终coverage和tasks；按已披露能力组织可执行取证，"
                        "未找到的能力不等于不存在，不虚构返回能力或宣称覆盖完整。"),
    }
    if remaining_rounds:
        # 第一回合优先确认必要能力，避免任务生成压力使模型跳过缺口发现。
        payload["required_schema"] = {"action": "discover_tools", "queries": [
            "按必需事实提出1至3条互补的业务能力检索短句；同次调用能取得的事实合并检索"]}
        payload["capability_search"]["instruction"] = (
            "本回合先确认完成目标所需的实际取证能力，优先只返回action=discover_tools及queries，"
            "不先生成正式任务。检索针对原始事实的获取能力，不包含最终解释或计算要求。"
            "只有匹配Skill已覆盖目标、上下文已有充分业务证据，或一个已披露候选明确能一次取得全部必要事实时，"
            "才可跳过补检索直接返回coverage和tasks；仅有通用主题描述不能作为完整覆盖依据。")
        payload["capability_search"]["request_schema"] = {
            "action": "discover_tools", "queries": ["缺少的必要取证能力短句，最多三条"]}
    return json.dumps(payload, ensure_ascii=False)
