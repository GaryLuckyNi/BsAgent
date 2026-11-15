from dataclasses import dataclass
from hashlib import sha256
from typing import Literal


@dataclass(frozen=True)
class CaptureDecision:
    status: Literal["captured", "not_sampled"]
    level: Literal["minimal", "detailed"] | None
    reason: str


@dataclass(frozen=True)
class TraceCaptureOutcome:
    failed: bool = False
    risk_level: str | None = None
    approval_occurred: bool = False
    verifier_status: str | None = None


class TraceCapturePolicy:
    def __init__(
        self,
        *,
        success_sample_ratio: float,
        detailed_sample_ratio: float,
    ) -> None:
        _validate_ratio(success_sample_ratio, name="success_sample_ratio")
        _validate_ratio(detailed_sample_ratio, name="detailed_sample_ratio")
        if detailed_sample_ratio > success_sample_ratio:
            raise ValueError(
                "detailed_sample_ratio 不能大于 success_sample_ratio"
            )
        self.success_sample_ratio = success_sample_ratio
        self.detailed_sample_ratio = detailed_sample_ratio

    def decide(
        self,
        *,
        trace_id: str,
        outcome: TraceCaptureOutcome,
    ) -> CaptureDecision:
        if not trace_id.strip():
            raise ValueError("trace_id 不能为空")
        forced_reason = _forced_capture_reason(outcome)
        if forced_reason is not None:
            return CaptureDecision(
                status="captured",
                level="detailed",
                reason=forced_reason,
            )

        sample_value = _stable_sample_value(trace_id)
        if sample_value < self.detailed_sample_ratio:
            return CaptureDecision(
                status="captured",
                level="detailed",
                reason="detailed_sampled",
            )
        if sample_value < self.success_sample_ratio:
            return CaptureDecision(
                status="captured",
                level="minimal",
                reason="success_sampled",
            )
        return CaptureDecision(
            status="not_sampled",
            level=None,
            reason="success_not_sampled",
        )


def _forced_capture_reason(outcome: TraceCaptureOutcome) -> str | None:
    if outcome.failed:
        return "run_failed"
    if outcome.risk_level == "high":
        return "high_risk"
    if outcome.approval_occurred:
        return "approval_occurred"
    if outcome.verifier_status != "pass":
        return "verifier_not_pass"
    return None


def _stable_sample_value(trace_id: str) -> float:
    digest_prefix = sha256(trace_id.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest_prefix, "big") / 2**64


def _validate_ratio(value: float, *, name: str) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{name} 必须在 0 到 1 之间")
