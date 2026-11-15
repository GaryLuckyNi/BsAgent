"""完整答复与分段发布共享的既有输出约束。"""

import re

_PROHIBITED_PATTERNS = (
    re.compile(r"(?:保证|确保|必然).{0,12}(?:收益|盈利|上涨)"),
    re.compile(r"(?:建议|应该|必须).{0,8}(?:买入|卖出|加仓|减仓)"),
)


def has_prohibited_advice(content: str) -> bool:
    return any(pattern.search(content) for pattern in _PROHIBITED_PATTERNS)
