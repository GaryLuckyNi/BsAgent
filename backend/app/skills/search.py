from collections.abc import Sequence

import bm25s

from app.skills.definitions import SkillCard
from app.tools.search.documents import tokenize_tool_text


class SkillSearchService:
    """对启用 Skill 的轻量卡片执行先过滤、后排序的 Top-K 检索。"""

    def __init__(self, cards: Sequence[SkillCard]) -> None:
        self._cards = tuple(cards)
        self._bm25 = self._build_index(self._cards)

    def search(
        self,
        *,
        query: str,
        domain: str,
        subdomain: str,
        top_k: int,
    ) -> list[SkillCard]:
        if top_k <= 0:
            return []
        eligible_ids = {
            index
            for index, card in enumerate(self._cards)
            if _matches_scope(card, domain=domain, subdomain=subdomain)
        }
        if not eligible_ids:
            return []

        exact_ids = [
            index
            for index in eligible_ids
            if self._cards[index].name.lower() in query.lower()
        ]
        if exact_ids:
            remaining = sorted(eligible_ids - set(exact_ids))
            selected = [*exact_ids, *remaining][:top_k]
            return [self._cards[index] for index in selected]

        ranked_ids = self._rank(query, eligible_ids=eligible_ids)
        return [self._cards[index] for index in ranked_ids[:top_k]]

    @staticmethod
    def _build_index(cards: Sequence[SkillCard]) -> bm25s.BM25 | None:
        if not cards:
            return None
        retriever = bm25s.BM25()
        retriever.index(
            [list(tokenize_tool_text(_search_text(card))) for card in cards],
            show_progress=False,
        )
        return retriever

    def _rank(self, query: str, *, eligible_ids: set[int]) -> list[int]:
        if self._bm25 is None:
            return []
        results = self._bm25.retrieve(
            [list(tokenize_tool_text(query))],
            k=len(self._cards),
            show_progress=False,
        )
        scored = [
            (int(document_id), float(score))
            for document_id, score in zip(
                results.documents[0], results.scores[0], strict=True
            )
            if int(document_id) in eligible_ids
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [document_id for document_id, _ in scored]


def _matches_scope(card: SkillCard, *, domain: str, subdomain: str) -> bool:
    domain_matches = not card.domains or domain in card.domains
    subdomain_matches = not card.subdomains or subdomain in card.subdomains
    return domain_matches and subdomain_matches


def _search_text(card: SkillCard) -> str:
    return " ".join(
        [card.name, card.summary, *card.domains, *card.subdomains]
    )
