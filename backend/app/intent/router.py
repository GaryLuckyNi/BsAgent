from app.intent.catalog import BusinessIntentCatalog
from app.intent.classifiers import LlmIntentClassifier, RuleOnlyIntentClassifier
from app.intent.rules import RulePreRouter
from app.intent.schemas import IntentDecisionModel, IntentSignal, RiskLevel
from app.llm.factory import LlmClient
from app.schemas.tools import ToolSpec


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
        catalog: BusinessIntentCatalog | None = None,
        llm_client: LlmClient | None = None,
        available_tools: list[ToolSpec] | None = None,
        rule_pre_router: RulePreRouter | None = None,
    ) -> None:
        self._catalog = catalog or BusinessIntentCatalog.load_default()
        self._llm_client = llm_client
        self._available_tools = available_tools or []
        self._rule_pre_router = rule_pre_router or RulePreRouter()
        self._rule_classifier = RuleOnlyIntentClassifier(catalog=self._catalog)

    def route(self, *, message: str, tenant_id: str, user_id: str) -> IntentDecisionModel:
        signal = self._rule_pre_router.analyze(message)
        if signal.hard_intent is not None:
            return self._evaluate(self._rule_classifier.classify(message=message, signal=signal), signal)

        rule_decision = self._rule_classifier.classify(message=message, signal=signal)
        if "rule_default_knowledge_qa" not in rule_decision.reason_codes:
            return self._evaluate(rule_decision, signal)

        if self._llm_client is not None:
            try:
                decision = LlmIntentClassifier(
                    llm_client=self._llm_client,
                    catalog=self._catalog,
                    available_tools=self._available_tools,
                ).classify(message=message, signal=signal)
                return self._evaluate(decision, signal)
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
        decision: IntentDecisionModel,
        signal: IntentSignal,
    ) -> IntentDecisionModel:
        if _risk_rank(signal.risk_floor) > _risk_rank(decision.risk_level):
            decision.risk_level = signal.risk_floor
            decision.reason_codes.append("risk_floor_applied")

        if decision.confidence < 0.55:
            decision.intent = "clarification_needed"
            decision.needs_clarification = True
            decision.reason_codes.append("confidence_below_threshold")
        elif decision.confidence < 0.7:
            decision.needs_clarification = True
            decision.reason_codes.append("confidence_needs_clarification")

        decision.next_node = "planner_agent"
        return decision


def _risk_rank(risk_level: RiskLevel) -> int:
    return {"low": 1, "medium": 2, "high": 3}[risk_level]
