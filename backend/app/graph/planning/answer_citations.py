"""将合成器的来源编号转换为已取得来源的公开 Markdown 链接。"""

import re
from collections.abc import Iterable, Mapping
from urllib.parse import quote, urlsplit

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
        self._urls = {source["source_id"]: source.get("url", "") for source in sources}

    def render(self, content: str) -> str:
        return _PART.sub(self._replace, content)

    @staticmethod
    def previews(sources: Iterable[Mapping]) -> list[dict]:
        """只公开已取得的来源信息，保留原始编号，不派生或补造网页内容。"""
        result = []
        for source in sources:
            identity = source.get("source_id", "")
            if not isinstance(identity, str) or not re.fullmatch(r"source_[1-9]\d*", identity):
                continue
            url = source.get("url", "")
            if not isinstance(url, str) or not _web_url(url):
                continue
            item = {"source_id": identity, "index": int(identity.removeprefix("source_")), "url": url}
            for key, limit in (("title", 500), ("snippet", 2000), ("published_at", 100)):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    item[key] = value.strip()[:limit]
            result.append(item)
        return result

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
    if not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False
