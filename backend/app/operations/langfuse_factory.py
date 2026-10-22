"""API 与 worker 共用的 Langfuse 收尾器构造，导入时不初始化应用。"""

from typing import Any

from app.core.config import Settings
from app.llm.deepseek import DeepSeekClient
from app.operations.langfuse_annotation import AnnotationQueueClient, ReviewPolicy
from app.operations.langfuse_evaluation import DeepSeekJudge
from app.operations.langfuse_finalize import LangfuseRunFinalizer
from app.operations.langfuse_scores import ensure_score_configs


def build_langfuse_finalizer(
    config: Settings,
    langfuse_runtime: Any | None,
) -> LangfuseRunFinalizer | None:
    if not config.langfuse_enabled or langfuse_runtime is None:
        return None
    client = getattr(langfuse_runtime, "client", None)
    judge = None
    if config.langfuse_deepseek_judge_enabled and config.deepseek_api_key:
        try:
            judge_client = DeepSeekClient(
                api_key=config.deepseek_api_key,
                model=config.deepseek_model,
                base_url=config.deepseek_base_url,
                timeout_seconds=config.langfuse_deepseek_judge_timeout_seconds,
            )
            judge = DeepSeekJudge(
                judge_client,
                timeout_seconds=config.langfuse_deepseek_judge_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - evaluator setup is best-effort
            judge = None
    annotation_client = None
    if config.langfuse_annotation_enabled:
        try:
            annotation_client = AnnotationQueueClient(
                config.langfuse_base_url,
                config.langfuse_public_key,
                config.langfuse_secret_key,
                retry_attempts=config.langfuse_annotation_retry_attempts,
            )
        except Exception:  # noqa: BLE001 - annotation setup is best-effort
            annotation_client = None
    return LangfuseRunFinalizer(
        client=client,
        judge=judge,
        annotation_client=annotation_client,
        score_config_provider=(
            lambda: ensure_score_configs(client) if client is not None else {}
        ),
        review_policy=ReviewPolicy(
            answer_threshold=config.langfuse_answer_review_threshold,
            selection_confidence_threshold=config.langfuse_selection_confidence_threshold,
            sample_rate=config.langfuse_annotation_sample_rate,
        ),
        enabled=True,
        evaluator_enabled=config.langfuse_evaluation_enabled,
        judge_enabled=config.langfuse_deepseek_judge_enabled,
        annotation_enabled=config.langfuse_annotation_enabled,
        environment=config.app_env,
        background=True,
    )
