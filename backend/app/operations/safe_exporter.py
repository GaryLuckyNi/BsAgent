"""应用受管 OTLP span 的最终副本边界；不修改原 span，不接管独立 exporter。"""

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, Status, TraceState

from app.security.observation import observation_view


def _attributes(value):
    return observation_view(dict(value or {}))


def _json_shape(value):
    if isinstance(value, dict):
        return {key: _json_shape(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_shape(item) for item in value]
    return value


def _safe_context(context):
    if context is None:
        return None
    state = [
        (key, value)
        for key, value in context.trace_state.items()
        if observation_view({key: value}) == {key: value}
    ]
    return SpanContext(
        context.trace_id, context.span_id, context.is_remote, context.trace_flags, TraceState(state)
    )


def safe_span(span):
    scope = span.instrumentation_scope
    return ReadableSpan(
        name=observation_view(span.name),
        context=_safe_context(span.context),
        parent=_safe_context(span.parent),
        resource=Resource(
            _attributes(span.resource.attributes), observation_view(span.resource.schema_url)
        ),
        attributes=_attributes(span.attributes),
        events=[
            Event(
                observation_view(event.name),
                {
                    key: "[异常正文已扣留]"
                    if key in {"exception.message", "exception.stacktrace"}
                    else value
                    for key, value in _attributes(event.attributes).items()
                },
                timestamp=event.timestamp,
            )
            for event in span.events
        ],
        links=[
            Link(_safe_context(link.context), _attributes(link.attributes)) for link in span.links
        ],
        kind=span.kind,
        status=Status(
            span.status.status_code, "operation_failed" if span.status.description else None
        ),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=InstrumentationScope(
            observation_view(scope.name),
            observation_view(scope.version),
            observation_view(scope.schema_url),
            _attributes(scope.attributes),
        )
        if scope
        else None,
    )


class SafeSpanExporter(SpanExporter):
    def __init__(self, exporter):
        self.exporter = exporter

    def export(self, spans):
        checked = []
        for span in spans:
            try:
                checked.append(safe_span(span))
            except Exception:  # noqa: BLE001, S112 - 不可转换的 span 丢弃，不另行打印原对象。
                continue
        return self.exporter.export(checked)

    def shutdown(self):
        return self.exporter.shutdown()

    def force_flush(self, timeout_millis=30_000):
        return self.exporter.force_flush(timeout_millis)


def should_export_langfuse_span(span):
    """SDK 属性 mask 无法改写 event/status/resource；有不安全字段则整条扣留。"""
    from app.llm.errors import LLM_ERROR_MESSAGES
    from app.search.base import WEB_SEARCH_ERROR_MESSAGES

    fixed = {
        *LLM_ERROR_MESSAGES.values(),
        *LLM_ERROR_MESSAGES,
        *WEB_SEARCH_ERROR_MESSAGES,
        "operation_failed",
        "模型输出内容受限。",
        "模型输出检查暂不可用。",
    }
    try:
        for context in (span.context, span.parent, *(link.context for link in span.links)):
            if context and _safe_context(context).trace_state != context.trace_state:
                return False
        if span.status.description and span.status.description not in fixed:
            return False
        for event in span.events:
            attrs = dict(event.attributes or {})
            if attrs.get("exception.message") and attrs["exception.message"] not in fixed:
                return False
            # 栈可能含代码行里的原始凭证；没有可靠的固定错误栈白名单。
            if attrs.get("exception.stacktrace"):
                return False
        other = {
            "name": span.name,
            "events": [
                {"name": event.name, "attrs": dict(event.attributes or {})} for event in span.events
            ],
            "resource": dict(span.resource.attributes),
            "schema": span.resource.schema_url,
            "scope": {
                "name": span.instrumentation_scope.name,
                "version": span.instrumentation_scope.version,
                "schema": span.instrumentation_scope.schema_url,
                "attrs": dict(span.instrumentation_scope.attributes or {}),
            }
            if span.instrumentation_scope
            else None,
            "links": [dict(link.attributes or {}) for link in span.links],
        }
        return observation_view(other) == _json_shape(other)
    except Exception:  # noqa: BLE001
        return False


def fixed_error_status(error):
    from app.llm.errors import LLM_ERROR_MESSAGES
    from app.search.base import WEB_SEARCH_ERROR_MESSAGES

    code = getattr(error, "code", None)
    return (
        code
        if isinstance(code, str) and code in {*LLM_ERROR_MESSAGES, *WEB_SEARCH_ERROR_MESSAGES}
        else "operation_failed"
    )
