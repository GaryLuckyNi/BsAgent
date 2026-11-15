"""真实 Langfuse 评测样本：离线构建、幂等发布及回读验证，不执行模型或工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.operations.langfuse_content import LangfuseContentPolicy

VERSION = "bsagent-real-2026-11-01-v1"
NAMES = {"development": "bsagent-real-development-v1", "holdout": "bsagent-real-holdout-v1"}
POLICY = LangfuseContentPolicy(max_chars=2_000_000)
COMMON_REQUIRED = [
    "以原始问题和有效上下文为验收依据；模型生成的 goal、计划和自评不能自行增加必需条件。",
    "金额、比例、日期、主体和期间须与实际证据一致；推演须说明公式与假设，不把假设写成已核实事实。",
    "联网摘要只证明该摘要提供的内容；仅有标题或 URL 不足以证明具体事实；明确引用与结论的对应关系。",
]
COMMON_FORBIDDEN = [
    "编造数值、来源或日期，混用公司、年度、单季与累计、单位或指标口径。",
    "把请求成功或空数组当作取得数据；把未检索到材料写成该事实不存在。",
    "仅因缺少非必要指标或第二来源而否定开放式回答；把旧回答、goal 或模型自评分当作正确答案。",
    "对外正文罗列内部工具名、调用参数、执行字段或下一步工具操作建议。",
]


def decode_io(value: Any) -> Any:
    """兼容 API 返回 JSON 对象或一至多层 JSON 字符串。"""
    for _ in range(4):
        if not isinstance(value, str):
            break
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            break
        if decoded == value:
            break
        value = decoded
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def first_difference(left: Any, right: Any, path: str = "root") -> str:
    """只报告差异路径和类型，不把业务正文或凭证打印到诊断输出。"""
    if json_equal(left, right):
        return ""
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return f"{path}: keys local_only={sorted(left.keys() - right.keys())} remote_only={sorted(right.keys() - left.keys())}"
        for key in left:
            diff = first_difference(left[key], right[key], f"{path}.{key}")
            if diff:
                return diff
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for index, (a, b) in enumerate(zip(left, right)):
            diff = first_difference(a, b, f"{path}[{index}]")
            if diff:
                return diff
    return f"{path}: {type(left).__name__} != {type(right).__name__}"


def json_equal(left: Any, right: Any) -> bool:
    """仅容忍 JSON 浮点往返的尾数误差；整数、布尔、正文和字段严格比对。"""
    if (
        type(left) in {int, float}
        and type(right) in {int, float}
        and (isinstance(left, int) or isinstance(right, int))
    ):
        # JSON 只有 number 类型：1.0 经服务端往返成 1 仍是同一值。
        # 不将大整数先转 float，防止超过 IEEE-754 精度时误判相等。
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        return math.isclose(left, right, rel_tol=1e-14, abs_tol=1e-15)
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    return left == right


def has_truncation(value: Any) -> bool:
    value = decode_io(value)
    if isinstance(value, dict):
        if value.get("truncated") is True and ("content" in value or "retained_chars" in value):
            return True
        if value.get("finish_reason") == "length":
            return True
        return any(has_truncation(v) for v in value.values())
    if isinstance(value, list):
        return any(has_truncation(v) for v in value)
    return False


def normalize_observation(item: dict) -> dict:
    result = {
        k: item.get(k)
        for k in (
            "id",
            "traceId",
            "parentObservationId",
            "name",
            "type",
            "startTime",
            "endTime",
            "level",
            "statusMessage",
            "version",
            "environment",
            "model",
            "modelParameters",
        )
    }
    for field in ("input", "output"):
        result[field] = decode_io(item.get(field))
        if isinstance(result[field], dict) and "user_prompt" in result[field]:
            result[field] = {
                **result[field],
                "user_prompt": decode_io(result[field]["user_prompt"]),
            }
    result["metadata"] = {
        k: v
        for k, v in (item.get("metadata") or {}).items()
        if k
        in {
            "business_trace_id",
            "execution_segment",
            "langgraph_node",
            "langgraph_step",
            "content_policy_applied",
        }
    }
    return POLICY.sanitize(result)


def belongs_to(item: dict, node: str, by_id: dict) -> bool:
    """依实际父子关系识别合成调用，不用提示词中偶然出现的节点名称猜测。"""
    seen = set()
    while item and item.get("id") not in seen:
        seen.add(item.get("id"))
        if item.get("name") == node or item.get("metadata", {}).get("langgraph_node") == node:
            return True
        item = by_id.get(item.get("parentObservationId"))
    return False


def assign_splits(rows: list[dict], target_holdout: int = 10) -> dict[str, str]:
    """会话、近似问题族连通分组；宁可保留集小，也不把大组拆开凑比例。"""
    parents = {r["trace_id"]: r["trace_id"] for r in rows}

    def find(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    owners = {}
    for row in rows:
        # 缺失会话记录共同隔离，不能声称互相独立。
        for key in (("session", row.get("session_group") or "unknown"), ("family", row["family"])):
            if key in owners:
                parents[find(row["trace_id"])] = find(owners[key])
            owners[key] = row["trace_id"]
    groups = {}
    for row in rows:
        groups.setdefault(find(row["trace_id"]), []).append(row["trace_id"])
    splits = dict.fromkeys(parents, "development")
    count = 0
    for group in sorted(groups.values(), key=lambda g: (len(g), sorted(g))):
        if len(group) <= target_holdout - count and count + len(group) < len(rows):
            for key in group:
                splits[key] = "holdout"
            count += len(group)
    return splits


def build_case(root: dict, observations: list[dict], label: dict, split: str) -> dict:
    question = decode_io(root.get("input"))
    if not isinstance(question, str) or not question.strip():
        raise ValueError("真实问题必须是完整非空文本，不接受合成探针对象")
    if root.get("name") != "bs-agent.run" or root.get("metadata", {}).get(
        "execution_segment"
    ) not in {"initial", "resume"}:
        raise ValueError("不是用户业务运行")
    if (
        label["trace_id"] != root["traceId"]
        or not label.get("must_include")
        or not label.get("must_not")
    ):
        raise ValueError("缺少逐项验收标准或来源不匹配")
    obs = sorted(
        (normalize_observation(x) for x in observations),
        key=lambda x: (x.get("startTime") or "", x["id"]),
    )
    if not obs or any(x["traceId"] != root["traceId"] for x in obs):
        raise ValueError("快照为空或混入其他 trace")
    by_id = {x["id"]: x for x in obs}
    routers = [x for x in obs if x["name"] == "intent_router" and x.get("input")]
    synths = [
        x
        for x in obs
        if x["type"] == "GENERATION" and x.get("input") and belongs_to(x, "synthesizer", by_id)
    ]
    final = decode_io(root.get("output"))
    answer = final.get("content", "") if isinstance(final, dict) else ""
    flags = []
    if not routers:
        flags.append("missing_router_context")
    if not synths:
        flags.append("missing_synthesis_input")
    if not isinstance(answer, str) or not answer.strip():
        flags.append("missing_final_answer")
    if has_truncation(question) or any(has_truncation(x["input"]) for x in synths):
        flags.append("synthesis_input_truncated")
    if any(has_truncation(x.get("input")) or has_truncation(x.get("output")) for x in obs):
        flags.append("trace_contains_truncation")
    tool_calls = [
        x
        for x in obs
        if x["type"] == "TOOL" and (x.get("input") is not None or x.get("output") is not None)
    ]
    failed = [
        x["name"]
        for x in tool_calls
        if isinstance(x["output"], dict)
        and x["output"].get("status") in {"error", "timeout", "failed"}
    ]
    numeric = label.get("numeric_checks", [])
    for check in numeric:
        if check.get("basis") != "user_input_arithmetic":
            raise ValueError("数值标准须源于独立审核的题设计算，不能从旧回答复制")
    item = {
        "id": str(uuid5(NAMESPACE_URL, f"{VERSION}:{root['traceId']}")),
        "input": {
            "question": question,
            "as_of_utc": root["startTime"],
            "timezone": "Asia/Shanghai",
            "initial_context": routers[0]["input"] if routers else None,
            "synthesis_turns": [
                {
                    "observation_id": x["id"],
                    "input": x["input"],
                    "model": x.get("model"),
                    "model_parameters": x.get("modelParameters"),
                }
                for x in synths
            ],
            "tool_observations": [
                {k: x.get(k) for k in ("id", "name", "input", "output", "startTime", "endTime")}
                for x in tool_calls
            ],
        },
        "expected_output": {
            "rubric_version": VERSION,
            "must_include": COMMON_REQUIRED + label["must_include"],
            "must_not": COMMON_FORBIDDEN + label["must_not"],
            "numeric_checks": numeric,
            "reference_answer": None,
            "reference_status": "rubric_only_external_facts_not_independently_verified",
        },
        "metadata": {
            "dataset_version": VERSION,
            "split": split,
            "family": label["family"],
            "category": label["category"],
            "source_trace_id": root["traceId"],
            "source_observation_id": root["id"],
            "session_group": root.get("sessionId"),
            "review_status": "agent_curated_pending_domain_review",
            "human_reviewed": False,
            "baseline_answer_is_ground_truth": False,
            "baseline_output": POLICY.sanitize(final),
            "baseline_assessment": "ungraded",
            "failed_tools_observed": failed,
            "integrity_flags": flags,
            "observation_count": len(obs),
            "query_rubric_ready": True,
            "synthesis_replay_ready": bool(synths)
            and not {"missing_final_answer", "synthesis_input_truncated"}.intersection(flags),
            "baseline_groundedness_ready": bool(synths)
            and not {"missing_final_answer", "synthesis_input_truncated"}.intersection(flags)
            and not has_truncation(final),
            "full_chain_replay_ready": False,
            "full_chain_replay_limitation": "Trace 未保存完整 checkpoint、原绑定与所有工具契约，只可分析轨迹，不能据此保证全链路确定性重放。",
            "snapshot_sha256": digest(obs),
        },
    }
    item = POLICY.sanitize(item)
    item["metadata"]["artifact_sha256"] = digest(item)
    return item


def validate_items(items: list[dict]) -> None:
    if not items or len({x["id"] for x in items}) != len(items):
        raise ValueError("样本为空或 ID 重复")
    owners = {}
    for item in items:
        m = item["metadata"]
        if (
            digest({**item, "metadata": {k: v for k, v in m.items() if k != "artifact_sha256"}})
            != m["artifact_sha256"]
        ):
            raise ValueError("样本内容摘要不匹配")
        for group in (("session", m.get("session_group") or "unknown"), ("family", m["family"])):
            if group in owners and owners[group] != m["split"]:
                raise ValueError("调优集与保留集存在分组泄漏")
            owners[group] = m["split"]


def fetch_observations(http: Any, params: dict) -> list[dict]:
    """完整遍历游标；限额或游标重复时失败，不静默把前一页当全量。"""
    result = []
    seen = set()
    query = {**params, "limit": 100, "fields": "core,basic,io,metadata,model"}
    for _ in range(100):
        response = http.get("/api/public/v2/observations", params=query)
        response.raise_for_status()
        page = response.json()
        result.extend(page["data"])
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor:
            ids = [x["id"] for x in result]
            if len(set(ids)) != len(ids):
                raise ValueError("分页返回重复观察，需重新导出")
            return result
        if cursor in seen:
            raise ValueError("分页游标重复")
        seen.add(cursor)
        query["cursor"] = cursor
    raise ValueError("达到读取上限，禁止把不完整结果当作全量")


def fetch(folder: Path, labels_path: Path, settings: Any, from_time: str, to_time: str) -> dict:
    import httpx

    folder.mkdir(parents=True, exist_ok=True)
    query = {"fromStartTime": from_time, "toStartTime": to_time}
    with httpx.Client(
        base_url=settings.langfuse_base_url,
        auth=(settings.langfuse_public_key, settings.langfuse_secret_key),
        trust_env=False,
        timeout=60,
    ) as http:
        roots = fetch_observations(http, {**query, "name": "bs-agent.run"})
        normalized = [{**normalize_observation(x), "sessionId": x.get("sessionId")} for x in roots]
        (folder / "roots.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
        )
        labels = json.loads(labels_path.read_text())
        selected = {x["trace_id"] for x in labels}
        if not selected.issubset({x["traceId"] for x in roots}):
            raise ValueError("标签引用的 trace 不在指定导出范围")

        def snapshot(trace):
            rows = [
                normalize_observation(x)
                for x in fetch_observations(http, {**query, "traceId": trace})
            ]
            # 文件名取自服务端已确认存在的 trace ID，且禁止路径字符。
            if not trace.isalnum():
                raise ValueError("无效 trace ID")
            (folder / f"{trace}.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
            )
            return len(rows)

        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(snapshot, sorted(selected)))
    return {"scanned_roots": len(roots), "snapshots": len(counts), "observations": sum(counts)}


def build(folder: Path, labels_path: Path) -> dict:
    roots = {x["traceId"]: x for x in json.loads((folder / "roots.json").read_text())}
    labels = json.loads(labels_path.read_text())
    if len({x["trace_id"] for x in labels}) != len(labels):
        raise ValueError("筛选标签包含重复 trace")
    rows = [{**x, "session_group": roots[x["trace_id"]].get("sessionId")} for x in labels]
    splits = assign_splits(rows)
    items = [
        build_case(
            roots[x["trace_id"]],
            json.loads((folder / (x["trace_id"] + ".json")).read_text()),
            x,
            splits[x["trace_id"]],
        )
        for x in labels
    ]
    validate_items(items)
    (folder / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    report = {
        "version": VERSION,
        "scanned_roots": len(roots),
        "selected": len(items),
        "splits": dict(Counter(x["metadata"]["split"] for x in items)),
        "categories": dict(Counter(x["metadata"]["category"] for x in items)),
        "synthesis_replay_ready": sum(x["metadata"]["synthesis_replay_ready"] for x in items),
        "full_chain_replay_ready": 0,
        "integrity_flags": dict(
            Counter(flag for x in items for flag in x["metadata"]["integrity_flags"])
        ),
        "human_reviewed": 0,
        "numeric_checks": sum(len(x["expected_output"]["numeric_checks"]) for x in items),
        "items_sha256": digest(items),
    }
    selected_ids = {x["trace_id"] for x in labels}
    selected_questions = {decode_io(roots[x]["input"]) for x in selected_ids}
    audit = []
    for key, root in roots.items():
        question = decode_io(root.get("input"))
        if key in selected_ids:
            reason = "selected"
        elif root.get("metadata", {}).get("execution_segment") not in {
            "initial",
            "resume",
        } or not isinstance(question, str):
            reason = "probe_or_non_business_segment"
        elif not root.get("output"):
            reason = "missing_final_output"
        elif question in selected_questions:
            reason = "duplicate_question_run"
        else:
            reason = "outside_first_batch_coverage_quota"
        audit.append({"trace_id": key, "reason": reason})
    report["selection_audit"] = dict(Counter(x["reason"] for x in audit))
    (folder / "selection-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    )
    (folder / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def publish(client: Any, items: list[dict]) -> dict:
    validate_items(items)
    result = {}
    for split, name in NAMES.items():
        selected = [x for x in items if x["metadata"]["split"] == split]
        try:
            dataset = client.get_dataset(name)
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise
            client.create_dataset(
                name=name,
                description="真实业务 trace 的固定证据及需求验收标准；AI 整理，尚非人工金标。",
                metadata={
                    "dataset_version": VERSION,
                    "split": split,
                    "synthetic_only": False,
                    "human_reviewed": False,
                },
            )
            dataset = client.get_dataset(name)
        known = {str(x.id): x for x in dataset.items}
        created = 0
        for item in selected:
            if item["id"] in known:
                existing = known[item["id"]]
                if (
                    not json_equal(existing.input, item["input"])
                    or not json_equal(existing.expected_output, item["expected_output"])
                    or not json_equal(existing.metadata, item["metadata"])
                ):
                    diff = (
                        first_difference(item["input"], existing.input, "input")
                        or first_difference(
                            item["expected_output"], existing.expected_output, "expected_output"
                        )
                        or first_difference(item["metadata"], existing.metadata, "metadata")
                    )
                    raise ValueError("远端已有同 ID 的不同样本，禁止静默覆盖: " + diff)
                continue
            client.create_dataset_item(
                dataset_name=name,
                id=item["id"],
                input=item["input"],
                expected_output=item["expected_output"],
                metadata=item["metadata"],
                source_trace_id=item["metadata"]["source_trace_id"],
                source_observation_id=item["metadata"]["source_observation_id"],
            )
            created += 1
        remote = client.get_dataset(name)
        verified = {str(x.id): x for x in remote.items}
        if set(verified) != {x["id"] for x in selected}:
            raise ValueError("回读样本数量或 ID 不一致")
        for item in selected:
            found = verified[item["id"]]
            if (
                not json_equal(found.input, item["input"])
                or not json_equal(found.expected_output, item["expected_output"])
                or not json_equal(found.metadata, item["metadata"])
            ):
                diff = (
                    first_difference(item["input"], found.input, "input")
                    or first_difference(
                        item["expected_output"], found.expected_output, "expected_output"
                    )
                    or first_difference(item["metadata"], found.metadata, "metadata")
                )
                raise ValueError("回读内容与本地样本不一致: " + diff)
            if (
                found.source_trace_id != item["metadata"]["source_trace_id"]
                or found.source_observation_id != item["metadata"]["source_observation_id"]
            ):
                raise ValueError("回读来源关联不一致")
        result[split] = {
            "name": name,
            "id": remote.id,
            "created": created,
            "verified_items": len(verified),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "build", "publish"])
    parser.add_argument("--folder", type=Path, default=ROOT / "data/evaluation/2026-11-01")
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--from-time", default="2026-10-20T00:00:00Z")
    parser.add_argument("--to-time", default="2026-11-01T16:00:00Z")
    args = parser.parse_args()
    if args.command == "build":
        print(
            json.dumps(
                build(args.folder, args.labels or args.folder / "labels.json"), ensure_ascii=False
            )
        )
        return
    from app.core.config import Settings

    from langfuse import Langfuse

    settings = Settings(_env_file=ROOT / ".env.local")
    url = urlsplit(settings.langfuse_base_url)
    if (
        url.scheme != "http"
        or url.hostname not in {"localhost", "127.0.0.1", "::1"}
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("真实数据仅允许导入当前本机 Langfuse，禁止改向外部服务")
    if args.command == "fetch":
        print(
            json.dumps(
                fetch(
                    args.folder,
                    args.labels or args.folder / "labels.json",
                    settings,
                    args.from_time,
                    args.to_time,
                ),
                ensure_ascii=False,
            )
        )
        return
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        tracing_enabled=False,
    )
    try:
        report = publish(client, json.loads((args.folder / "items.json").read_text()))
        (args.folder / "publish-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
