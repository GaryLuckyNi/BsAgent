"""SDK 输出回调边界：禁用未完成 token，结束/错误回调只接收安全副本。"""

import inspect

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.callbacks.base import BaseCallbackManager
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult
from langchain_core.runnables.base import RunnableBindingBase, RunnableParallel, RunnableSequence
from langchain_core.runnables.config import ensure_config, merge_configs

from app.llm.errors import normalized_provider_error
from app.security.model_output import output_view, protect_message_response


def _checked(value):
    if isinstance(value, BaseMessage):
        return protect_message_response(value)
    if isinstance(value, LLMResult):
        return value.model_copy(
            update={
                "generations": [[_checked(item) for item in group] for group in value.generations],
                "llm_output": output_view(value.llm_output),
            }
        )
    if isinstance(value, Generation):
        update = {
            "text": output_view(value.text),
            "generation_info": output_view(value.generation_info),
        }
        if isinstance(value, ChatGeneration):
            update["message"] = protect_message_response(value.message)
        return value.model_copy(update=update)
    if isinstance(value, dict):
        return {
            key: _checked(item)
            if output_view({key: "public-probe"}).get(key) == "public-probe"
            else "***"
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_checked(item) for item in value]
    return output_view(value)


class OutputCallback(BaseCallbackHandler):
    def __init__(self, handler):
        self.handler = handler

    def __getattribute__(self, name):
        if name.startswith("on_"):
            handler = object.__getattribute__(self, "handler")
            target = getattr(handler, name)

            def prepare(args, kwargs):
                if name in {"on_llm_new_token", "on_text", "on_custom_event"}:
                    return None  # 不发布逐 token；最终安全正文由 on_llm_end/调用返回值提供。
                if name.endswith("_error"):
                    return (normalized_provider_error(args[0]),), {
                        key: value
                        for key, value in kwargs.items()
                        if key in {"run_id", "parent_run_id"}
                    }
                if name.endswith("_end"):
                    return tuple(_checked(arg) for arg in args), {
                        key: value if key in {"run_id", "parent_run_id"} else _checked(value)
                        for key, value in kwargs.items()
                    }
                return args, kwargs

            if inspect.iscoroutinefunction(target):

                async def async_call(*args, **kwargs):
                    checked = prepare(args, kwargs)
                    if checked is not None:
                        return await target(*checked[0], **checked[1])

                return async_call

            def call(*args, **kwargs):
                checked = prepare(args, kwargs)
                if checked is not None:
                    return target(*checked[0], **checked[1])

            return call
        if name.startswith("ignore_") or name in {"run_inline", "raise_error"}:
            return getattr(object.__getattribute__(self, "handler"), name)
        return object.__getattribute__(self, name)


def _callbacks(value):
    def wrap(handler):
        return handler if isinstance(handler, OutputCallback) else OutputCallback(handler)

    if isinstance(value, BaseCallbackManager):
        result = value.copy()
        by_id = {
            id(handler): wrap(handler) for handler in value.handlers + value.inheritable_handlers
        }
        result.handlers = [by_id[id(handler)] for handler in value.handlers]
        result.inheritable_handlers = [by_id[id(handler)] for handler in value.inheritable_handlers]
        return result
    return [wrap(handler) for handler in (value or [])]


def guarded_invocation(bound, config):
    stored = getattr(bound, "config", {})
    merged = merge_configs(stored, ensure_config(config))
    merged["callbacks"] = _callbacks(merged.get("callbacks"))
    if stored.get("callbacks") is not None:
        # 防止 RunnableBinding 合并时再次追加未包装的原 handler。
        bound = bound.model_copy(update={"config": {**stored, "callbacks": None}})
    return _guard_local_callbacks(bound), merged


def _guard_local_callbacks(bound):
    if isinstance(bound, BaseChatModel):
        return bound.model_copy(update={"callbacks": _callbacks(bound.callbacks)})
    if isinstance(bound, RunnableBindingBase):
        config = dict(bound.config)
        if config.get("callbacks") is not None:
            config["callbacks"] = _callbacks(config["callbacks"])
        return bound.model_copy(
            update={"bound": _guard_local_callbacks(bound.bound), "config": config}
        )
    if isinstance(bound, RunnableSequence):
        return bound.model_copy(
            update={
                "first": _guard_local_callbacks(bound.first),
                "middle": [_guard_local_callbacks(step) for step in bound.middle],
                "last": _guard_local_callbacks(bound.last),
            }
        )
    if isinstance(bound, RunnableParallel):
        return bound.model_copy(
            update={
                "steps__": {
                    name: _guard_local_callbacks(step) for name, step in bound.steps__.items()
                }
            }
        )
    return bound
