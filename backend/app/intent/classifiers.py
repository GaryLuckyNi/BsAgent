import json
import re
from collections.abc import Callable

from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import IntentDecisionModel
from app.llm.factory import LlmClient, invoke_llm_completion
from app.llm.schemas import LlmStreamDelta


class LlmIntentClassifier:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        catalog: BusinessIntentCatalog,
    ) -> None:
        self._llm_client = llm_client
        self._catalog = catalog

    def classify(
        self,
        *,
        message: str,
        context: dict | None = None,
        on_delta: Callable[[LlmStreamDelta], None] | None = None,
    ) -> IntentDecisionModel:
        result = invoke_llm_completion(
            self._llm_client,
            system_prompt=(
                "你是 BsAgent 的投研任务理解器，只能输出单行 JSON object。"
                "先判断请求是否属于 allowed_taxonomy 中启用的业务范围。"
                "范围内必须从同一条 taxonomy 记录选择 intent_type、domain、subdomain，"
                "并将用户问题忠实改写成 goal。不要改变用户问题的原意，"
                "不要丢失公司名称、证券代码、时间范围和用户明确提出的限制。"
                "范围外请求或寒暄输出 intent_type、domain、subdomain、goal 均为 null。"
                "不要选择 Skill 或 Tool，不要拆分任务，不要判断风险。"
            ),
            user_prompt=json.dumps(
                {
                    "message": message,
                    "context": context or {},
                    "allowed_taxonomy": self._catalog.prompt_taxonomy(),
                    "required_schema": {
                        "intent_type": "string|null",
                        "domain": "string|null",
                        "subdomain": "string|null",
                        "goal": "string|null",
                        "confidence": "number between 0 and 1",
                    },
                },
                ensure_ascii=False,
            ),
            temperature=0.0,
            max_tokens=512,
            thinking=False,
            on_delta=on_delta,
        )
        return IntentDecisionModel.model_validate(_extract_json_object(result.content))


def _extract_json_object(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match is None:
        raise ValueError("LLM intent output did not contain JSON object.")
    return json.loads(match.group(0))
