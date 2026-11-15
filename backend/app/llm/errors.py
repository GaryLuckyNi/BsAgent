"""模型传输错误只保留固定码、状态和超时阶段，不携带响应/请求对象。"""

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
)

from app.security.http_response import HttpResponseBudgetError
from app.security.run_budget import RunBudgetError, find_run_budget_error
from app.security.sdk_http import find_response_budget_error

LLM_ERROR_MESSAGES = {
    "llm_http_error": "模型服务请求失败。",
    "llm_timeout": "模型服务响应超时。",
    "llm_unreachable": "模型服务不可达。",
    "llm_invalid_response": "模型服务返回了无效响应。",
    "llm_provider_error": "模型服务暂不可用。",
    "llm_response_limit_exceeded": "模型服务响应超出读取限制。",
}


class LlmConfigurationError(RuntimeError):
    pass


class LlmProviderError(RuntimeError):
    def __init__(self, *, code, message=None, status_code=None, timeout_phase=None):
        self.code = code if code in LLM_ERROR_MESSAGES else "llm_provider_error"
        self.message = LLM_ERROR_MESSAGES[self.code]
        self.status_code = status_code if isinstance(status_code, int) else None
        self.timeout_phase = (
            timeout_phase
            if timeout_phase in {"connect", "read", "write", "pool", "unknown"}
            else None
        )
        super().__init__(self.message)


def normalized_provider_error(error):
    run_error = find_run_budget_error(error)
    if run_error is not None:
        return type(run_error)()
    budget_error = find_response_budget_error(error)
    if budget_error is not None:
        return LlmProviderError(
            code="llm_timeout"
            if budget_error.code == "http_response_deadline"
            else "llm_response_limit_exceeded",
        )
    if isinstance(
        error,
        (
            ValueError,
            RecursionError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
            APIResponseValidationError,
        ),
    ):
        return LlmProviderError(code="llm_invalid_response")
    if isinstance(error, (httpx.HTTPStatusError, APIStatusError)):
        status = (
            error.response.status_code
            if isinstance(error, httpx.HTTPStatusError)
            else error.status_code
        )
        return LlmProviderError(code="llm_http_error", status_code=status)
    if isinstance(error, (httpx.TimeoutException, APITimeoutError)):
        phase = next(
            (
                name
                for cls, name in (
                    (httpx.ConnectTimeout, "connect"),
                    (httpx.ReadTimeout, "read"),
                    (httpx.WriteTimeout, "write"),
                    (httpx.PoolTimeout, "pool"),
                )
                if isinstance(error, cls)
            ),
            "unknown",
        )
        return LlmProviderError(code="llm_timeout", timeout_phase=phase)
    if isinstance(error, APIConnectionError):
        return LlmProviderError(code="llm_unreachable")
    if isinstance(error, APIError):
        return LlmProviderError(code="llm_invalid_response")
    return LlmProviderError(code="llm_unreachable")


# SDK在网络读取后还会解析/验证响应；输入/选项检查保留在调用前，不吞掉安全拒绝。
PROVIDER_TRANSPORT_ERRORS = (
    httpx.HTTPError,
    APIError,
    HttpResponseBudgetError,
    RunBudgetError,
    ValueError,
    RecursionError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
)
