import re
from typing import Literal


SensitivityLevel = Literal["low", "medium", "high"]


_HIGH_SENSITIVITY_PATTERNS = [
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:api[_-]?key|token|secret|password|密码|口令)\b", re.IGNORECASE),
]


def detect_sensitivity(text: str) -> SensitivityLevel:
    for pattern in _HIGH_SENSITIVITY_PATTERNS:
        if pattern.search(text):
            return "high"
    return "low"
