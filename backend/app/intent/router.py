from collections.abc import Callable
import logging

from app.intent.catalog import BusinessIntentCatalog
from app.intent.classifiers import LlmIntentClassifier, RuleOnlyIntentClassifier
from app.intent.rules import RulePreRouter
from app.intent.schemas import (
    BusinessIntentProfile,
    IntentRoutingResult,
    IntentSignal,
    RiskLevel,
)
from app.llm.factory import LlmClient
from app.skills.definitions import SkillCard


logger = logging.getLogger(__name__)


class IntentClassificationError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        http_status: int = 503,
    ) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class HybridIntentRouter:
    def __init__(
        self,
        *,
        catalog: BusinessIntentCatalog,
        llm_client: LlmClient | None = None,
        available_skills_provider: Callable[[str], list[SkillCard]] | None = None,
        rule_pre_router: RulePreRouter | None = None,
    ) -> None:
        if catalog is None:
            raise TypeError("catalog must not be None")
        self._catalog = catalog
        self._llm_client = llm_client
        self._available_skills_provider = available_skills_provider
        self._rule_pre_router = rule_pre_router or RulePreRouter()
        self._rule_classifier = RuleOnlyIntentClassifier(catalog=self._catalog)

    def route(
        self,
        *,
        message: str,
        tenant_id: str,
        user_id: str,
        context: dict | None = None,
    ) -> IntentRoutingResult:
        signal = self._rule_pre_router.analyze(message)
        if signal.hard_intent is not None:
            return self._evaluate(
                IntentRoutingResult(
                    decision=self._rule_classifier.classify(
                        message=message,
                        signal=signal,
                    )
                ),
                signal,
            )

        matched_profile = self._catalog.match_by_keywords(message)
        rule_decision = self._rule_classifier.classify(message=message, signal=signal)
        if matched_profile is not None and matched_profile.intent != "task_workflow":
            return self._evaluate(
                IntentRoutingResult(decision=rule_decision),
                signal,
                matched_profile=matched_profile,
            )

        if self._llm_client is not None:
            try:
                available_skills = self._available_skills(tenant_id)
                result = LlmIntentClassifier(
                    llm_client=self._llm_client,
                    catalog=self._catalog,
                    available_skills=available_skills,
                ).classify(
                    message=message,
                    signal=signal,
                    context=context,
                    matched_profile=matched_profile,
                )
                return self._evaluate(
                    result,
                    signal,
                    matched_profile=matched_profile,
                )
            except Exception as exc:
                raise IntentClassificationError(
                    code="intent_classifier_failed",
                    message=f"LLM intent classifier failed: {exc}",
                    http_status=502,
                ) from exc

        raise IntentClassificationError(
            code="intent_classifier_not_configured",
            message=(
                "Intent classifier LLM is required when no deterministic intent rule matches. "
                "Set BS_AGENT_LLM_PROVIDER=deepseek and configure the provider before routing."
            ),
            http_status=503,
        )

    def _evaluate(
        self,
        result: IntentRoutingResult,
        signal: IntentSignal,
        *,
        matched_profile: BusinessIntentProfile | None = None,
    ) -> IntentRoutingResult:
        decision = result.decision
        risk_floor = signal.risk_floor
        if matched_profile is not None and _risk_rank(
            matched_profile.risk_level
        ) > _risk_rank(risk_floor):
            risk_floor = matched_profile.risk_level
        if _risk_rank(risk_floor) > _risk_rank(decision.risk_level):
            decision.risk_level = risk_floor
            decision.reason_codes.append("risk_floor_applied")

        if decision.confidence < 0.55:
            decision.intent = "clarification_needed"
            decision.needs_clarification = True
            decision.reason_codes.append("confidence_below_threshold")
        elif decision.confidence < 0.7:
            decision.needs_clarification = True
            decision.reason_codes.append("confidence_needs_clarification")

        decision.next_node = "planner_agent"
        return result

    def _available_skills(self, tenant_id: str) -> list[SkillCard]:
        if self._available_skills_provider is None:
            return []
        try:
            return self._available_skills_provider(tenant_id)
        except Exception:
            logger.warning(
                "intent_skill_cards_unavailable",
                exc_info=True,
            )
            return []


def _risk_rank(risk_level: RiskLevel) -> int:
    return {"low": 1, "medium": 2, "high": 3}[risk_level]
