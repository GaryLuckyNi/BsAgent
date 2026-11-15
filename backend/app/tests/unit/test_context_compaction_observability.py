import logging

from app.context_compaction.observability import ContextCompactionObserver


class Instrument:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def add(self, value, attributes):
        self.calls.append((value, attributes))

    def record(self, value, attributes):
        self.calls.append((value, attributes))


class Meter:
    def __init__(self):
        self.instruments = {}

    def create_counter(self, name):
        return self._instrument(name)

    def create_histogram(self, name):
        return self._instrument(name)

    def _instrument(self, name):
        instrument = Instrument(name)
        self.instruments[name] = instrument
        return instrument


def test_observer_records_safe_low_cardinality_metrics_and_logs(caplog):
    meter = Meter()
    observer = ContextCompactionObserver(meter=meter)
    secret = "api_key=secret-value"

    with caplog.at_level(logging.INFO, logger="app.context_compaction.observability"):
        observer.record(
            "trigger",
            session_key=f"session-{secret}",
            trigger="hard_sync",
            ratio=0.86,
            prompt=secret,
        )
        observer.record(
            "success",
            session_key=f"session-{secret}",
            trigger="hard_sync",
            status="activated",
            compaction_id="cmp-safe",
            version=2,
            source_tokens=4_000,
            summary_tokens=800,
            ratio=0.2,
            duration_seconds=0.25,
            summary=secret,
        )
        observer.record(
            "failure",
            session_key=f"session-{secret}",
            trigger="soft_async",
            reason="generation_error",
            status="failed",
            raw_message=secret,
        )
        observer.record(
            "race_discard",
            session_key=f"session-{secret}",
            trigger="soft_async",
            reason="active_changed",
            status="race_discard",
        )
        observer.record(
            "degraded",
            session_key=f"session-{secret}",
            reason="compaction_failed",
            status="degraded",
        )

    serialized_logs = "\n".join(str(record.__dict__) for record in caplog.records)
    assert secret not in serialized_logs
    assert "session-api_key" not in serialized_logs
    assert "cmp-safe" in serialized_logs
    assert "context_compaction_event" in serialized_logs

    for instrument in meter.instruments.values():
        for _, attributes in instrument.calls:
            assert set(attributes) <= {"trigger", "reason", "status"}
            assert secret not in str(attributes)
            assert "session" not in str(attributes)
            assert "compaction_id" not in attributes

    assert meter.instruments["context_compaction_trigger"].calls
    assert meter.instruments["context_compaction_success"].calls
    assert meter.instruments["context_compaction_failure"].calls
    assert meter.instruments["context_compaction_race_discard"].calls
    assert meter.instruments["context_compaction_degraded_run"].calls
    assert meter.instruments["context_compaction_duration_seconds"].calls
    assert meter.instruments["context_compaction_source_tokens"].calls
    assert meter.instruments["context_compaction_summary_tokens"].calls
    assert meter.instruments["context_compaction_ratio"].calls
