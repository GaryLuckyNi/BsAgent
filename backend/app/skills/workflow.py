"""从唯一维护入口 summary 编译固定步骤并冻结执行身份。"""

import json
import re
from hashlib import sha256
from typing import Any

from app.graph.planning.json_output import extract_json_object
from app.llm.factory import invoke_llm_completion
from app.skills.workflow_contracts import SkillWorkflow, WorkflowDraft, WorkflowError, WorkflowStep
from app.tools.named_lookup import resolve_named_tool

_SYSTEM_PROMPT = """你是 Skill 工作流编译器。summary 是唯一流程来源，不执行工具、不回答业务问题。
将所有步骤按原顺序转换成 JSON；每次工具调用单独一个步骤，纯分析步骤 tool_name=null。
工具名必须来自 summary，保留完整名或原始短名；未明确工具的外部数据步骤必须报错，不能改为纯分析或擅自推荐工具。
source 必须逐字引用该步骤原文。required_fields 仅提取原文明示的必需字段，嵌套字段用点路径、数组用 *。
completion_criteria 只记录“该步原始工具结果必须已满足，否则停止”的事实条件，必须逐字引用 summary；不添加默认业务要求。
结果筛选、排序、去重、空值展示、字段裁剪和最终表格列都属于 output_requirement，不得用于判定原始工具返回是否已经加工完成。
缺失处理默认 missing_policy=stop；只有原文明示允许该步缺失后继续时才为 continue，并用 missing_policy_source 逐字引用依据。
固定工具失败不允许自行换工具或重规划；只有 summary 明示允许该步骤失败后使用 web_search 兜底时，才在 web_search_fallback_source 逐字引用允许依据，否则为 null。不得把禁止条款、其他步骤的许可或普通缺失后继续当作搜索授权。
条件性搜索兜底保留在该工具步骤上，不拆成每次必执行的独立步骤。
output_requirement 提取最终数据整理、格式及约束，使用 summary 中包含这些要求的连续原文。不得遗漏步骤、改变顺序、增加工具或绕过审批。
仅返回 JSON：{"steps":[{"instruction":"本步目标和参数约束","source":"原文","tool_name":"明确的工具名或null","required_fields":[],"completion_criteria":[],"missing_policy":"stop","missing_policy_source":null,"web_search_fallback_source":null}],"output_requirement":"输出要求"}。
无法确定固定工作流时返回 {"error":"具体缺失或歧义"}。"""


def _mentioned(value: str, text: str) -> bool:
    # 工具标识符只使用 ASCII 字母、数字、点、下划线和连字符。不能使用
    # ``\w`` 做边界判断，因为 Unicode 下中文也属于 ``\w``，会漏掉
    # “调用first” 这类自然的中文写法。
    return (
        re.search(
            r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_.-])",
            text,
        )
        is not None
    )


def _explicit_tools(source: str, registry) -> list[str]:
    """返回步骤原文中明确出现的已注册工具，供漏绑校验使用。"""
    mentioned: list[str] = []
    for spec in registry.specs():
        short_name = spec.name.rsplit(".", 1)[-1]
        if _mentioned(spec.name, source) or _mentioned(short_name, source):
            mentioned.append(spec.name)
    return mentioned


def compile_workflow(
    *, definition, parent_task_id: str, llm_client, registry, permission_scopes: tuple[str, ...]
) -> SkillWorkflow:
    payload = {"summary": definition.description}
    last_error = None
    for _ in range(2):
        response = invoke_llm_completion(
            llm_client,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False),
            temperature=0.0,
            max_tokens=6000,
            thinking=False,
        )
        try:
            draft = WorkflowDraft.model_validate(extract_json_object(response.content))
            if draft.output_requirement and draft.output_requirement not in definition.description:
                raise WorkflowError("最终输出要求必须引用 summary 原文。")
            steps = []
            for index, item in enumerate(draft.steps):
                if item.source not in definition.description:
                    raise WorkflowError("步骤原文不在 summary 中。")
                if any(
                    not criterion or criterion not in definition.description
                    for criterion in item.completion_criteria
                ):
                    raise WorkflowError("完成条件必须引用 summary 原文。")
                if item.missing_policy == "continue" and (
                    not item.missing_policy_source
                    or item.missing_policy_source not in definition.description
                ):
                    raise WorkflowError("允许缺失后继续必须有 summary 原文依据。")
                for field in item.required_fields:
                    if (
                        not re.fullmatch(r"[\w]+(?:\.(?:[\w]+|\*))*", field)
                        or field.split(".")[-1] not in item.source
                    ):
                        raise WorkflowError("必需字段必须有步骤原文依据。")
                tool = None
                if item.web_search_fallback_source and (
                    not item.tool_name
                    or item.web_search_fallback_source not in definition.description
                    or not _mentioned("web_search", item.web_search_fallback_source)
                ):
                    raise WorkflowError("搜索兜底必须有 summary 中显式允许 web_search 的原文依据。")
                if item.tool_name:
                    if not _mentioned(item.tool_name, item.source):
                        raise WorkflowError("工具名称必须在该步骤原文明确出现。")
                    tool = resolve_named_tool(
                        registry, item.tool_name, permission_scopes=permission_scopes
                    )
                elif _explicit_tools(item.source, registry):
                    raise WorkflowError("summary 明确声明工具，但步骤未绑定工具。")
                elif item.required_fields:
                    raise WorkflowError("无工具分析步骤不能声明工具输出字段。")
                task_id = (
                    "sw_"
                    + sha256(
                        f"{parent_task_id}:{definition.definition_hash}:{index}".encode()
                    ).hexdigest()[:32]
                )
                steps.append(
                    WorkflowStep(
                        **{**item.model_dump(), "tool_name": tool.name if tool else None},
                        task_id=task_id,
                        tool_version=tool.version if tool else None,
                    )
                )
            declared_step_markers = re.findall(r"第\s*\d+\s*步", definition.description)
            for marker in declared_step_markers:
                if not any(marker in step.source for step in steps):
                    raise WorkflowError(f"编译结果遗漏 summary 中声明的步骤：{marker}。")
            return SkillWorkflow(
                parent_task_id=parent_task_id,
                skill_name=definition.name,
                skill_version=definition.version,
                definition_hash=definition.definition_hash,
                summary=definition.description,
                output_requirement=draft.output_requirement,
                steps=steps,
            )
        except (ValueError, KeyError) as exc:
            last_error = exc
            # 工具缺失、歧义或权限不足不能让模型通过换工具“修复”。
            if isinstance(exc, WorkflowError) and exc.code != "skill_workflow_invalid":
                raise
            payload["repair"] = str(exc)
    detail = f"：{last_error}" if last_error else "。"
    raise WorkflowError(
        "无法将 summary 解析为明确工作流，请补充步骤、工具名称和完成要求" + detail
    ) from last_error


def missing_fields(value: Any, fields: list[str]) -> list[str]:
    def present(current, path):
        if not path:
            return current is not None and current != "" and current != [] and current != {}
        part, *rest = path
        if isinstance(current, list) and part != "*":
            # 表格型 MCP 常直接返回根数组。此时未写通配符的字段表示
            # “结果集中存在该列的有效值”；需要逐行强校验时仍显式使用 *。
            return bool(current) and any(present(item, path) for item in current)
        if part == "*":
            return (
                isinstance(current, list)
                and bool(current)
                and all(present(item, rest) for item in current)
            )
        return isinstance(current, dict) and part in current and present(current[part], rest)

    return [field for field in fields if not present(value, field.split("."))]
