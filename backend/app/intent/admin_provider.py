from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.intent.schemas import BusinessIntentProfile


class BusinessIntentProviderError(RuntimeError):
    code = "business_intent_provider_invalid_response"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("业务意图服务返回无效响应。")


class _AdminBusinessIntentListPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]]
    total: int = Field(ge=0)


class AdminBusinessIntentProvider:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def list_profiles(self) -> list[BusinessIntentProfile]:
        try:
            response = self._client.get(
                "/api/runtime/business-intents",
                params={"enabled": "true"},
            )
            response.raise_for_status()
            payload = _AdminBusinessIntentListPayload.model_validate(response.json())
            profile_fields = set(BusinessIntentProfile.model_fields)
            return [
                BusinessIntentProfile.model_validate(
                    {key: value for key, value in item.items() if key in profile_fields}
                )
                for item in payload.items
            ]
        except (httpx.HTTPError, ValueError, ValidationError):
            raise BusinessIntentProviderError() from None

    def close(self) -> None:
        self._client.close()
