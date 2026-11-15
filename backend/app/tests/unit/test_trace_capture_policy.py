from hashlib import sha256

import pytest

from app.harness.capture_policy import TraceCaptureOutcome, TraceCapturePolicy


def _sample_value(trace_id: str) -> float:
    digest_prefix = sha256(trace_id.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest_prefix, "big") / 2**64


def test_capture_policy_uses_stable_sha256_sampling():
    trace_id = "trace_stable"
    sample_value = _sample_value(trace_id)
    policy = TraceCapturePolicy(
        success_sample_ratio=min(sample_value + 0.01, 1),
        detailed_sample_ratio=max(sample_value - 0.01, 0),
    )
    outcome = TraceCaptureOutcome(verifier_status="pass")

    first = policy.decide(trace_id=trace_id, outcome=outcome)
    second = policy.decide(trace_id=trace_id, outcome=outcome)

    assert first == second
    assert first.status == "captured"
    assert first.level == "minimal"
    assert first.reason == "success_sampled"


def test_capture_policy_marks_detailed_sample_at_lower_threshold():
    trace_id = "trace_detailed"
    sample_value = _sample_value(trace_id)
    ratio = min(sample_value + 0.01, 1)
    policy = TraceCapturePolicy(
        success_sample_ratio=ratio,
        detailed_sample_ratio=ratio,
    )

    decision = policy.decide(
        trace_id=trace_id,
        outcome=TraceCaptureOutcome(verifier_status="pass"),
    )

    assert decision.status == "captured"
    assert decision.level == "detailed"
    assert decision.reason == "detailed_sampled"


def test_capture_policy_does_not_capture_unsampled_success():
    policy = TraceCapturePolicy(
        success_sample_ratio=0,
        detailed_sample_ratio=0,
    )

    decision = policy.decide(
        trace_id="trace_not_sampled",
        outcome=TraceCaptureOutcome(verifier_status="pass"),
    )

    assert decision.status == "not_sampled"
    assert decision.level is None
    assert decision.reason == "success_not_sampled"


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (TraceCaptureOutcome(failed=True), "run_failed"),
        (TraceCaptureOutcome(risk_level="high"), "high_risk"),
        (TraceCaptureOutcome(approval_occurred=True), "approval_occurred"),
        (TraceCaptureOutcome(verifier_status="fail"), "verifier_not_pass"),
    ],
)
def test_capture_policy_forces_capture_for_important_outcomes(outcome, reason):
    policy = TraceCapturePolicy(
        success_sample_ratio=0,
        detailed_sample_ratio=0,
    )

    decision = policy.decide(trace_id="trace_forced", outcome=outcome)

    assert decision.status == "captured"
    assert decision.level == "detailed"
    assert decision.reason == reason


def test_capture_policy_rejects_detailed_ratio_above_success_ratio():
    with pytest.raises(ValueError, match="detailed_sample_ratio"):
        TraceCapturePolicy(
            success_sample_ratio=0.1,
            detailed_sample_ratio=0.2,
        )


@pytest.mark.parametrize("verifier_status", [None, "unknown", "PASS"])
def test_capture_policy_forces_capture_unless_verifier_explicitly_passes(
    verifier_status,
):
    policy = TraceCapturePolicy(
        success_sample_ratio=0,
        detailed_sample_ratio=0,
    )
    outcome = TraceCaptureOutcome(verifier_status=verifier_status)

    decision = policy.decide(trace_id="trace_verifier", outcome=outcome)

    assert decision.status == "captured"
    assert decision.level == "detailed"
    assert decision.reason == "verifier_not_pass"


def test_capture_outcome_defaults_to_unknown_verifier_status():
    assert TraceCaptureOutcome().verifier_status is None


@pytest.mark.parametrize("trace_id", ["", "   ", "\t\n"])
def test_capture_policy_rejects_blank_trace_id(trace_id):
    policy = TraceCapturePolicy(
        success_sample_ratio=1,
        detailed_sample_ratio=0,
    )

    with pytest.raises(ValueError, match="trace_id"):
        policy.decide(
            trace_id=trace_id,
            outcome=TraceCaptureOutcome(verifier_status="pass"),
        )


def test_capture_policy_success_ratio_one_captures_every_success():
    policy = TraceCapturePolicy(
        success_sample_ratio=1,
        detailed_sample_ratio=0,
    )

    decision = policy.decide(
        trace_id="trace_ratio_one",
        outcome=TraceCaptureOutcome(verifier_status="pass"),
    )

    assert decision.status == "captured"
    assert decision.level == "minimal"
    assert decision.reason == "success_sampled"
