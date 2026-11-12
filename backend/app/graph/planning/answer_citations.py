"""将合成器的来源编号转换为已取得来源的公开 Markdown 链接。"""

import re
from collections.abc import Iterable, Mapping
from itertools import islice
from urllib.parse import quote

from app.security.public_output import public_sources, public_text, public_web_url

_SOURCE = r"source\\?_[1-9]\d*"
_PART = re.compile(
    # 代码、已有链接及 URL 原样保留，不改写其中的标识符或目标。
    r"(?P<protected>(?P<fence>`{3,}|~{3,})[\s\S]*?(?:(?P=fence)|\Z)"
    r"|(?P<ticks>`+)[^\n]*?(?P=ticks)"
    # 连续的 [source_1][source_2] 是两个引用，不当作引用式链接。
    rf"|(?!\[{_SOURCE}\]\[{_SOURCE}\])!?\[[^\]\n]*\](?:\([^\n]*?\)|\[[^\]\n]*\])"
    r"|https?://[^\s<>]+)"
    rf"|\[(?P<bracket>{_SOURCE})\]"
    rf"|(?<![\w/])(?P<source>{_SOURCE})(?!\w)"
)


class AnswerCitations:
    def __init__(self, sources: Iterable[Mapping[str, str]]):
        self._urls = {source["source_id"]: public_web_url(source.get("url")) or ""
                      for source in islice(sources,200) if isinstance(source,Mapping)
                      and isinstance(source.get("source_id"),str)}

    def render(self, content: str) -> str:
        return public_text(_PART.sub(self._replace, content))

    @staticmethod
    def previews(sources: Iterable[Mapping]) -> list[dict]:
        """只公开已取得的来源信息，保留原始编号，不派生或补造网页内容。"""
        return public_sources([dict(source) for source in islice(sources,200) if isinstance(source,Mapping)])

    def _replace(self, match: re.Match) -> str:
        if match["protected"] is not None:
            return match[0]
        source_id = (match["source"] or match["bracket"]).replace("\\", "")
        if source_id not in self._urls:
            return "来源未核实"
        url = self._urls[source_id]
        if not _web_url(url):
            return "来源链接不可用"
        # 尖括号目标允许 URL 中的括号；其余可终止 Markdown 的字符编码。
        target = quote(url, safe=":/?#[]@!$&'()*+,;=%~._-")
        number = source_id.removeprefix("source_")
        return f"[{number}](<{target}>)"


def _web_url(url: str) -> bool:
    return public_web_url(url) is not None
