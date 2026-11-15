from typing import Protocol

from app.graph.state import AgentState, ImageAnalysisSummary
from app.security.principal import Principal


MAX_STATE_OCR_CHARS = 2000


class AttachmentContextResolver(Protocol):
    def resolve(
        self,
        asset_ids: list[str],
        *,
        principal: Principal,
        session_id: str,
    ) -> list[ImageAnalysisSummary]: ...


def build_attachment_analyzer_node(*, resolver: AttachmentContextResolver):
    def node(state: AgentState) -> AgentState:
        asset_ids = [ref["asset_id"] for ref in state["attachment_refs"]]
        if not asset_ids:
            state["image_analyses"] = []
            return state

        resolved = resolver.resolve(
            asset_ids,
            principal=Principal(
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                scopes=tuple(state["permission_scopes"]),
            ),
            session_id=state["session_id"],
        )
        analyses = [_apply_state_budget(analysis) for analysis in resolved]
        state["attachment_refs"] = [
            {
                "asset_id": analysis["asset_id"],
                "analysis_id": analysis["analysis_id"],
                "status": analysis["status"],
            }
            for analysis in analyses
        ]
        state["image_analyses"] = analyses
        return state

    return node


def _apply_state_budget(
    analysis: ImageAnalysisSummary,
) -> ImageAnalysisSummary:
    extracted_text = analysis["extracted_text"][:MAX_STATE_OCR_CHARS]
    return {
        **analysis,
        "extracted_text": extracted_text,
        "truncated": (
            analysis["truncated"]
            or len(extracted_text) < len(analysis["extracted_text"])
        ),
    }
