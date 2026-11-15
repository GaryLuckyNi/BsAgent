"""Stable score definitions and privacy-safe Langfuse score writes."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from langfuse.api.commons.types import ConfigCategory, ScoreConfigDataType

logger = logging.getLogger("bs_agent.langfuse.scores")


class _ScoreConfigTypeConflict(ValueError):
    pass


@dataclass(frozen=True)
class ScoreSpec:
    name: str
    data_type: str
    min_value: float | None = None
    max_value: float | None = None
    categories: tuple[tuple[str, float], ...] = ()

    @property
    def config(self) -> dict[str, list[dict[str, str | float]]]:
        return {
            "categories": [{"label": label, "value": value} for label, value in self.categories]
        }


SCORE_SPECS: Mapping[str, ScoreSpec] = MappingProxyType(
    {
        "task_success": ScoreSpec("task_success", "BOOLEAN"),
        "answer_quality": ScoreSpec("answer_quality", "NUMERIC", 0, 1),
        "groundedness": ScoreSpec("groundedness", "NUMERIC", 0, 1),
        "selection_correctness": ScoreSpec("selection_correctness", "BOOLEAN"),
        "citation_coverage": ScoreSpec("citation_coverage", "NUMERIC", 0, 1),
        "action_policy_compliance": ScoreSpec("action_policy_compliance", "BOOLEAN"),
        "review_outcome": ScoreSpec(
            "review_outcome",
            "CATEGORICAL",
            categories=(("accept", 1), ("needs_revision", 0.5), ("reject", 0)),
        ),
    }
)


def _as_list(response: Any) -> list[Any]:
    data = getattr(response, "data", response)
    if isinstance(data, Mapping):
        return list(data.values())
    return list(data or [])


def _enum_value(value: Any) -> str:
    """Normalize SDK enums and plain strings to the stable spec spelling."""
    return str(getattr(value, "value", value)).upper()


def ensure_score_configs(client: Any) -> dict[str, str]:
    """Return config ids, creating missing stable configs idempotently."""
    try:
        existing = _as_list(client.api.score_configs.get())
        by_name = {getattr(config, "name", None): config for config in existing}
        result: dict[str, str] = {}
        for name, spec in SCORE_SPECS.items():
            config = by_name.get(name)
            if config is not None:
                if _enum_value(getattr(config, "data_type", "")) != spec.data_type:
                    raise _ScoreConfigTypeConflict(
                        f"Score config {name!r} has a different data type"
                    )
                result[name] = str(config.id)
                continue
            kwargs: dict[str, Any] = {
                "name": name,
                "data_type": ScoreConfigDataType(spec.data_type),
            }
            if spec.min_value is not None:
                kwargs.update(min_value=spec.min_value, max_value=spec.max_value)
            if spec.categories:
                kwargs["categories"] = [
                    ConfigCategory(value=value, label=label) for label, value in spec.categories
                ]
            created = client.api.score_configs.create(**kwargs)
            result[name] = str(created.id)
            by_name[name] = created
        return result
    except _ScoreConfigTypeConflict:
        raise
    except Exception:  # noqa: BLE001 - telemetry setup is best effort
        logger.warning("langfuse_score_config_sync_failed")
        return {}


def record_langfuse_score(
    client: Any,
    *,
    name: str,
    value: bool | float | str,
    config_id: str,
    trace_id: str,
    evaluator: str,
    evaluator_version: str,
    rubric_version: str,
    run_id: str,
    dataset_version: str | None = None,
    observation_id: str | None = None,
    environment: str | None = None,
) -> bool:
    """Write a score; telemetry failures never affect business execution."""
    spec = SCORE_SPECS[name]
    if name == "selection_correctness" and not observation_id:
        raise ValueError("selection_correctness requires observation_id")
    if name != "selection_correctness" and observation_id is not None:
        raise ValueError("observation_id is only supported for selection_correctness")
    if spec.data_type == "BOOLEAN" and not isinstance(value, bool):
        raise ValueError(f"{name} requires a boolean value")
    if spec.data_type == "NUMERIC" and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
    ):
        raise ValueError(f"{name} requires a numeric value from 0 to 1")
    if spec.data_type == "CATEGORICAL":
        values = dict(spec.categories)
        if isinstance(value, str):
            if value not in values:
                raise ValueError(f"Unsupported {name} category")
            value = values[value]
        elif value not in values.values():
            raise ValueError(f"Unsupported {name} category value")

    metadata = {
        "evaluator": evaluator,
        "evaluator_version": evaluator_version,
        "rubric_version": rubric_version,
        "dataset_version": dataset_version or "none",
        "run_id": run_id,
    }
    kwargs: dict[str, Any] = {
        "name": name,
        "value": value,
        "data_type": spec.data_type,
        "config_id": config_id,
        "trace_id": trace_id,
        "metadata": metadata,
    }
    if observation_id is not None:
        kwargs["observation_id"] = observation_id
    if environment is not None:
        kwargs["environment"] = environment
    try:
        client.create_score(**kwargs)
        return True
    except Exception:  # noqa: BLE001 - telemetry writes are best effort
        logger.warning("langfuse_score_write_failed")
        return False


write_score = record_langfuse_score
