"""从本地原始 trace 回放来源链接，不请求模型、搜索或数据库。"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.graph.planning.answer_citations import AnswerCitations
from app.graph.planning.evidence_projection import EvidenceProjection
from app.graph.planning.synthesis_decision import parse_synthesis_decision
from app.graph.planning.synthesis_stream import SynthesisStream
from app.llm.schemas import LlmStreamDelta


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def main():
    rows = decode((ROOT / "data/investigations/2026-10-28-tool-query-coverage/observations.json").read_text())["data"]
    generation = next(row for row in rows if row["id"] == "86d2091e5a7dbb13")
    prompt = decode(generation["input"])["user_prompt"]
    # 观测正文在 20000 字截断，但 research_materials 完整；按实际材料顺序
    # 重建来源表，并逐项比对原始 source_ids，避免依赖观察返回的倒序时间。
    assert prompt["truncated"] is True
    materials, _ = json.JSONDecoder().raw_decode(prompt["content"].split('"research_materials": ', 1)[1])
    projection = EvidenceProjection()
    for material in materials:
        tool = next(row for row in rows if row["name"] == "web_search"
                    and decode(row["input"])["purpose"] == material["research_goal"])
        projected = projection.content(decode(tool["output"])["result"])
        assert projected["source_ids"] == material["content"]["source_ids"]

    original = decode(generation["output"])["content"]
    answer = parse_synthesis_decision(original).answer
    render = AnswerCitations(projection.sources).render
    converted = render(answer)
    source_map = {source["source_id"]: source for source in projection.sources}
    cited_ids = re.findall(r"source_[1-9]\d*", answer)
    assert cited_ids
    for source_id in cited_ids:
        assert source_id in source_map
        assert render(source_id) in converted
    assert "source_" not in converted
    assert "来源未核实" not in converted
    events = []
    stream = SynthesisStream(on_delta=events.append, transform=render)
    for char in original:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.finish()
    assert "".join(event.delta for event in events) == converted

    record = {
        "trace_id": "e63644e40326c60cd3b2c2ab910d5ad8",
        "sources_total": len(source_map), "citation_occurrences": len(cited_ids),
        "unique_cited_sources": len(set(cited_ids)), "stream_equals_final": True,
        "sources_reconstructed_and_ids_verified": True,
        "citations": [{key: source_map[source_id][key] for key in ("source_id", "title", "url")}
                      for source_id in dict.fromkeys(cited_ids)],
        "original_answer": answer, "linked_answer": converted,
    }
    output = Path(__file__).with_name("2026-10-28-answer-source-links.json")
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in record.items()
                      if key not in {"citations", "original_answer", "linked_answer"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
