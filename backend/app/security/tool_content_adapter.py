"""公共工具设施适配：内容视图不改变执行状态，策略故障时只扣留内容。"""

import json

from app.schemas.tools import ToolCallResult
from app.security.prompt_injection import SIGNAL_NAMES
from app.security.tool_content import ToolContentMetadata


def project_tool_result(
    result,
    *,
    policy,
    tool_name,
    sink,
    context=None,
    schema=None,
    binding_digest=None,
    preserve_host_metadata=False,
):
    values = {
        "tool_name": tool_name,
        "sink": sink,
        "tenant_id": getattr(context, "tenant_id", None),
        "scopes": getattr(context, "permission_scopes", ()),
        "binding_digest": binding_digest,
    }
    try:
        content = policy.project(
            result.result, schema=schema if result.status == "success" else None, **values
        )
        error = result.error
        views = [content]
        if error is not None:
            details = policy.project(error.details, **values)
            message = policy.project(error.message, **values)
            views.extend([details, message])
            error = error.model_copy(
                update={"details": details.data or {}, "message": message.data or "工具内容受限。"}
            )
        dispositions = {view.metadata.disposition for view in views}
        metadata = content.metadata.model_copy(
            update={
                "disposition": "withheld"
                if "withheld" in dispositions
                else "redacted"
                if "redacted" in dispositions
                else "allowed",
                "applied_rule_ids": tuple(
                    sorted({name for view in views for name in view.metadata.applied_rule_ids})
                ),
                "risk_signals": tuple(
                    sorted({name for view in views for name in view.metadata.risk_signals})
                ),
            }
        )
        if preserve_host_metadata and isinstance(result.content_policy, ToolContentMetadata):
            previous = result.content_policy
            rank = {"allowed": 0, "redacted": 1, "withheld": 2}
            metadata = metadata.model_copy(
                update={
                    "disposition": max((metadata.disposition, previous.disposition), key=rank.get),
                    "applied_rule_ids": tuple(
                        sorted(set(metadata.applied_rule_ids) | set(previous.applied_rule_ids))
                    ),
                    "risk_signals": tuple(
                        sorted(set(metadata.risk_signals) | set(previous.risk_signals))
                    ),
                }
            )
        return result.model_copy(
            update={"result": content.data, "error": error, "content_policy": metadata}
        )
    except Exception:  # noqa: BLE001 - 内容处理故障不能丢弃已执行写入的真实状态。
        metadata = ToolContentMetadata(
            policy_version=getattr(policy, "version", "unavailable"),
            disposition="withheld",
            business_rules_configured=bool(getattr(policy, "rules", ())),
            source_tool=tool_name,
            binding_digest=binding_digest,
            risk_signals=("content_policy_unavailable",),
        )
        error = (
            result.error.model_copy(update={"message": "工具内容策略暂不可用。", "details": {}})
            if result.error
            else None
        )
        return result.model_copy(
            update={"result": None, "error": error, "content_policy": metadata}
        )


def diagnostic_without_payload(raw):
    # 不在 raw_result/Trace/补写队列中另存一份工具正文。
    return {
        "kind": "mapping"
        if isinstance(raw, dict)
        else "tool_result"
        if isinstance(raw, ToolCallResult)
        else "other",
        "original_payload_retained": False,
    }


def project_tool_message(*, content, artifact, tool_name, policy):
    try:
        if len(content) > policy.max_string_chars:
            value = content
        else:
            try:
                value = json.loads(content)
            except (ValueError, TypeError):
                value = content
        view = policy.project(value, tool_name=tool_name, sink="workflow")
        data = view.data
        metadata = view.metadata.model_dump(mode="json")
        execution = {}
        if isinstance(artifact, dict) and all(
            name in artifact for name in ("tool_call_id", "tool_name", "status", "trace_id")
        ):
            parsed = ToolCallResult.model_validate(artifact)
            safe_artifact = project_tool_result(
                parsed, policy=policy, tool_name=tool_name, sink="workflow"
            ).model_dump(mode="json")
            execution = {"tool_status": parsed.status, "execution_state": parsed.execution_state}
            previous = parsed.content_policy
            if isinstance(previous, ToolContentMetadata):
                # Artifact 只能增加限制，不能把外部自报元数据升级为宿主授权。
                rank = {"allowed": 0, "redacted": 1, "withheld": 2}
                metadata["disposition"] = max(
                    (metadata["disposition"], previous.disposition), key=rank.get
                )
                known_signals = SIGNAL_NAMES | {
                    "instruction_override",
                    "authority_claim",
                    "secret_exfiltration",
                    "content_limit",
                    "content_policy_unavailable",
                }
                metadata["risk_signals"] = sorted(
                    set(metadata["risk_signals"]) | (set(previous.risk_signals) & known_signals)
                )
                rule_ids = {rule.rule_id for rule in policy.rules}
                metadata["applied_rule_ids"] = sorted(
                    set(metadata["applied_rule_ids"]) | (set(previous.applied_rule_ids) & rule_ids)
                )
                if previous.disposition == "withheld":
                    data = None
                safe_artifact["content_policy"] = dict(metadata)
        else:
            safe_artifact = policy.project(artifact, tool_name=tool_name, sink="workflow").data
        metadata.update(execution)
        body = (
            {**data, "_tool_content_boundary": metadata}
            if isinstance(data, dict)
            else {"data": data, "_tool_content_boundary": metadata}
        )
        return json.dumps(body, ensure_ascii=False, allow_nan=False), safe_artifact, metadata
    except Exception:  # noqa: BLE001 - 模型入口处理失败时扣留原文。
        metadata = {
            "trust": "untrusted_tool_data",
            "source_tool": tool_name,
            "disposition": "withheld",
            "policy_version": "unavailable",
        }
        return json.dumps({"data": None, "_tool_content_boundary": metadata}), None, metadata
