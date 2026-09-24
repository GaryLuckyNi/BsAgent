from collections.abc import Callable

from app.intent.catalog import BusinessIntentCatalog
from app.intent.classifiers import LlmIntentClassifier
from app.intent.schemas import IntentDecisionModel
from app.llm.factory import LlmClient
from app.llm.schemas import LlmStreamDelta


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
        llm_client: LlmClient | None,
    ) -> None:
        if catalog is None:
            raise TypeError("catalog must not be None")
        self._catalog = catalog
        self._llm_client = llm_client

    def route(
        self,
        *,
        message: str,
        tenant_id: str,
        user_id: str,
        context: dict | None = None,
        on_delta: Callable[[LlmStreamDelta], None] | None = None,
    ) -> IntentDecisionModel:
        del tenant_id, user_id
        if self._llm_client is None:
            raise IntentClassificationError(
                code="intent_classifier_not_configured",
                message="Intent classifier LLM is required.",
                http_status=503,
            )
        try:
            decision = LlmIntentClassifier(
                llm_client=self._llm_client,
                catalog=self._catalog,
            ).classify(message=message, context=context, on_delta=on_delta)
        except Exception as exc:
            raise IntentClassificationError(
                code="intent_classifier_failed",
                message="LLM intent classifier failed.",
                http_status=502,
            ) from exc

        if decision.intent_type is None:
            return decision
        if not self._catalog.allows(
            intent_type=decision.intent_type,
            domain=decision.domain or "",
            subdomain=decision.subdomain or "",
        ):
            raise IntentClassificationError(
                code="intent_catalog_value_invalid",
                message="LLM intent classifier returned a value outside the catalog.",
                http_status=502,
            )
        return decision
