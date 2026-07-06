from dataclasses import dataclass


@dataclass
class EvalCaseResult:
    name: str
    passed: bool
    score: float


class EvalHook:
    def __init__(self) -> None:
        self.results: list[EvalCaseResult] = []

    def record_case(self, *, name: str, passed: bool, score: float = 1.0) -> None:
        self.results.append(EvalCaseResult(name=name, passed=passed, score=score))
