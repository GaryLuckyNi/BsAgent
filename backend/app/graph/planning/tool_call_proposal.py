"""只校验规划草案，不检索、不派发工具，也不生成执行身份。"""

from collections.abc import Callable

from langchain_core.messages import AIMessage

from app.graph.planning.contracts import SubTaskModel
from app.tools.candidate_resolver import ToolCandidateResolution
from app.tools.execution_gateway import validate_tool_arguments
from app.tools.input_schema import model_input_schema
from app.tools.native_calls import NativeToolSelection
from app.tools.planning_candidates import PlanningToolSnapshot
from app.tools.redaction import reject_inline_credentials, reject_server_injected_arguments


def adopt_tool_call_proposal(*, state: dict, task: SubTaskModel,
                            resolution: ToolCandidateResolution,
                            canonicalize: Callable[[NativeToolSelection], NativeToolSelection],
                            ) -> tuple[NativeToolSelection | None, str]:
    proposal = task.proposed_tool_call
    if proposal is None:
        return None, "not_provided"
    if task.depends_on or task.skill_name is not None or task.execution_type != "tool":
        return None, "ineligible_task"
    if proposal.candidate_ref not in task.tool_candidate_refs or resolution.source != "planning_reuse":
        return None, "candidate_unavailable"
    raw_snapshot = state.get("planning_tool_candidates")
    if raw_snapshot is None:
        return None, "schema_not_disclosed"
    snapshot = PlanningToolSnapshot.model_validate(raw_snapshot)
    if snapshot.request_id != state["request_id"] or snapshot.catalog_digest != state["tool_catalog_digest"]:
        return None, "candidate_unavailable"
    card = next((card for card in snapshot.candidates if card.ref == proposal.candidate_ref), None)
    if card is None or card.input_schema is None:
        return None, "schema_not_disclosed"
    spec = next((spec for spec in resolution.candidates
                 if (spec.name, spec.version) == (card.tool_name, card.tool_version)), None)
    if spec is None or spec.risk_level not in {"low", "medium"}:
        return None, "candidate_unavailable"
    if card.input_schema != model_input_schema(spec):
        return None, "schema_not_disclosed"
    arguments = dict(proposal.arguments)
    try:
        reject_inline_credentials(arguments)
        reject_server_injected_arguments(arguments, schema=spec.input_schema,
                                         server_injected_fields=spec.server_injected_fields)
        selection = canonicalize(NativeToolSelection(selected_tool=spec, message=AIMessage(
            content="", tool_calls=[{"id": "planner-proposal", "name": spec.name, "args": arguments}],
        )))
        if not validate_tool_arguments(tool_spec=spec,
                                       arguments=selection.message.tool_calls[0]["args"]).valid:
            return None, "invalid_arguments"
    except ValueError:
        return None, "invalid_arguments"
    return selection, "accepted"
