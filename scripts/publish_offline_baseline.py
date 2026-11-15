"""将历史回答基线关联到本机 Langfuse 实验；不覆盖既有业务评分。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings

from scripts.offline_answer_baseline import DIMENSIONS, PROMPT_PATH, VERSION
from scripts.real_eval_dataset import NAMES, decode_io, digest, json_equal, validate_items

PREFIX = "offline_baseline_v4.model"
VALUES = {"pass": 1.0, "partial": 0.5, "fail": 0.0}


def score_records(item: dict, result: dict, review: dict | None = None) -> list[dict]:
    review = review or {}
    excluded = review.get("excluded_dimensions", {})
    if not isinstance(excluded, dict) or set(excluded) - set(DIMENSIONS):
        raise ValueError("复核排除项含未知维度")
    if any(not isinstance(reason, str) or not reason.strip() for reason in excluded.values()):
        raise ValueError("复核排除项必须说明理由")
    if result["is_counterfactual_control"]:
        raise ValueError("控制例不得发布为真实业务评分")
    if (
        result["source_trace_id"] != item["metadata"]["source_trace_id"]
        or result["item_id"] != item["id"]
        or result["source_observation_id"] != item["metadata"]["source_observation_id"]
    ):
        raise ValueError("评审与样本来源不匹配")
    if result["evaluator_version"] != VERSION or result["prompt_sha256"] != digest(
        PROMPT_PATH.read_text()
    ):
        raise ValueError("评审版本不一致")
    assessments = result.get("judgment", {}).get("dimensions", {})
    metrics = [("evaluation_valid", float(result["status"] == "success"), None)]
    for dim in DIMENSIONS:
        assessment = assessments.get(dim)
        if (
            dim not in excluded
            and result["status"] == "success"
            and assessment
            and assessment["verdict"] in VALUES
        ):
            metrics.append((dim, VALUES[assessment["verdict"]], assessment["verdict"]))
    return [
        {
            "name": PREFIX + "." + name,
            "value": value,
            "data_type": "NUMERIC",
            "score_id": str(uuid5(NAMESPACE_URL, f"{VERSION}:{item['id']}:{name}")),
            "trace_id": result["source_trace_id"],
            "observation_id": result["source_observation_id"],
            "environment": "offline-evaluation",
            "comment": assessments[name]["reason"]
            if name in assessments
            else "仅表示评审是否有效；不表示回答质量。",
            "metadata": {
                "evaluator_version": VERSION,
                "prompt_sha256": result["prompt_sha256"],
                "dataset_item_id": item["id"],
                "verdict": verdict,
                "model": result.get("model"),
                "human_reviewed": False,
                "review_status": "model_preliminary",
                "quality_score_not_ground_truth": True,
                "scope": "historical_answers_cross_versions",
                "result_sha256": digest(result),
                "agent_review_sha256": digest(review),
                "excluded_dimensions": excluded,
            },
        }
        for name, value, verdict in metrics
    ]


def fetch_scores(client, name: str) -> dict:
    scores = {}
    cursor = None
    seen = set()
    for _ in range(100):
        kwargs = {
            "name": name,
            "environment": "offline-evaluation",
            "limit": 100,
            "fields": "core,details,subject",
        }
        if cursor:
            kwargs["cursor"] = cursor
        response = client.api.scores_v3.get_many_v3(**kwargs)
        for row in response.data:
            scores[row.id] = row
        meta = getattr(response, "meta", None)
        cursor = getattr(meta, "cursor", None)
        if not cursor:
            return scores
        if cursor in seen:
            raise ValueError("评分读取游标重复")
        seen.add(cursor)
    raise ValueError("评分分页超过上限")


def verify_score(found, expected):
    subject = getattr(found, "subject", None)
    if (
        float(found.value) != expected["value"]
        or subject is None
        or subject.kind != "observation"
        or subject.trace_id != expected["trace_id"]
        or subject.id != expected["observation_id"]
        or found.metadata != expected["metadata"]
    ):
        raise ValueError("评分回读或既有评分与本地结果不同，禁止覆盖")


def fetch_experiment_items(client, dataset_id: str, *, version: str = VERSION) -> list:
    rows = []
    cursor = None
    seen = set()
    for _ in range(100):
        response = client.api.experiments.list_items(
            from_start_time=datetime(2026, 11, 1, tzinfo=UTC),
            dataset_id=dataset_id,
            experiment_name=version,
            fields="core,dataset,io,metadata,experimentMetadata",
            limit=100,
            **({"cursor": cursor} if cursor else {}),
        )
        rows.extend(response.data)
        cursor = getattr(response.meta, "cursor", None)
        if not cursor:
            return rows
        if cursor in seen:
            raise ValueError("实验读取游标重复")
        seen.add(cursor)
    raise ValueError("实验分页超过上限")


def frozen_output(item: dict) -> dict:
    return {
        "artifact_kind": "frozen_historical_answer_not_new_generation",
        "source_trace_id": item["metadata"]["source_trace_id"],
        "source_observation_id": item["metadata"]["source_observation_id"],
        "content": item["metadata"]["baseline_output"]["content"],
    }


def publish(
    client,
    items: list[dict],
    results: list[dict],
    reviews: dict | None = None,
    *,
    run_version: str = VERSION,
    score_factory=None,
    output_factory=None,
    metadata_overrides: dict | None = None,
    description: str = "固定历史回答离线初评；输出是旧答案快照，并非新生成或业务重放。",
) -> dict:
    score_factory = score_factory or score_records
    output_factory = output_factory or frozen_output
    validate_items(items)
    reviews = reviews or {}
    if set(reviews) - {x["metadata"]["source_trace_id"] for x in items}:
        raise ValueError("复核包含本次集合之外的来源")
    by_id = {x["item_id"]: x for x in results}
    if len(by_id) != len(results) or set(by_id) != {x["id"] for x in items}:
        raise ValueError("基线结果缺失、重复或混入控制例")
    # 在写入前校验所有结果，禁止用控制例或来源不匹配的判断创建实验。
    for item in items:
        score_factory(item, by_id[item["id"]], reviews.get(item["metadata"]["source_trace_id"]))
    metadata = {
        "evaluator_version": VERSION,
        "prompt_sha256": digest(PROMPT_PATH.read_text()),
        "results_sha256": digest(sorted(results, key=lambda x: x["item_id"])),
        "agent_reviews_sha256": digest(reviews),
        "scope": "historical_answers_cross_versions",
        "human_reviewed": "false",
        "not_current_deployment_accuracy": "true",
        "output_kind": "frozen_historical_answer_not_new_generation",
        **(metadata_overrides or {}),
    }
    runs = []
    links = {}
    for split, name in NAMES.items():
        selected = {x["id"]: x for x in items if x["metadata"]["split"] == split}
        if not selected:
            continue
        dataset = client.get_dataset(name)
        remote_items = {x.id: x for x in dataset.items}
        for key, item in selected.items():
            remote = remote_items.get(key)
            if remote is None or not all(
                json_equal(getattr(remote, field), item[field])
                for field in ("input", "expected_output", "metadata")
            ):
                raise ValueError("远端样本与固定基线不一致")
        rows = fetch_experiment_items(client, dataset.id, version=run_version)

        def verify_rows(rows, selected=selected):
            known = {}
            for row in rows:
                key = row.experiment_item_id
                if key not in selected or key in known:
                    raise ValueError("实验含未知或重复样本")
                if not json_equal(
                    decode_io(row.output), output_factory(selected[key])
                ) or not json_equal(decode_io(row.input), selected[key]["input"]):
                    raise ValueError("实验输入输出与固定历史记录不同，禁止覆盖")
                if row.experiment_metadata != metadata:
                    raise ValueError("已存在同名不同版本的实验，禁止覆盖")
                known[key] = row
            return known

        known = verify_rows(rows)
        pending = [remote_items[key] for key in selected if key not in known]
        if pending:
            # 仅包装既有答案，task 内没有模型、网页或业务工具调用。
            def task(*, item, selected=selected):
                return output_factory(selected[item.id])

            client.run_experiment(
                name=run_version,
                run_name=run_version,
                description=description,
                data=pending,
                task=task,
                evaluators=[],
                max_concurrency=3,
                metadata=metadata,
            )
            client.flush()
            for attempt in range(10):
                known = verify_rows(fetch_experiment_items(client, dataset.id, version=run_version))
                if set(known) == set(selected):
                    break
                if attempt < 9:
                    sleep(1)
        if set(known) != set(selected):
            raise ValueError("实验尚未全部可见；稍后回读，禁止盲目重复创建")
        experiment_ids = {row.experiment_id for row in known.values()}
        if len(experiment_ids) != 1:
            raise ValueError("同名实验出现多个ID")
        for key, row in known.items():
            links[key] = {"trace_id": row.trace_id, "observation_id": row.id}
        runs.append(
            {
                "dataset": name,
                "dataset_id": dataset.id,
                "run_id": next(iter(experiment_ids)),
                "run_name": run_version,
                "verified_items": len(known),
                "new_experiment_items": len(pending),
            }
        )
    records = []
    for item in items:
        for record in score_factory(
            item, by_id[item["id"]], reviews.get(item["metadata"]["source_trace_id"])
        ):
            record["metadata"].update(
                source_trace_id=record["trace_id"],
                source_observation_id=record["observation_id"],
            )
            record.update(links[item["id"]])
            records.append(record)
    names = {x["name"] for x in records}
    existing = {k: v for name in names for k, v in fetch_scores(client, name).items()}
    created = 0
    for record in records:
        if record["score_id"] in existing:
            verify_score(existing[record["score_id"]], record)
            continue
        client.create_score(**record)
        created += 1
    client.flush()
    missing = set()
    for attempt in range(10):
        stored = {k: v for name in names for k, v in fetch_scores(client, name).items()}
        missing = {r["score_id"] for r in records} - set(stored)
        if not missing:
            for record in records:
                verify_score(stored[record["score_id"]], record)
            break
        if attempt < 9:
            sleep(1)
    if missing:
        raise ValueError(f"评分尚未全部可见，缺少 {len(missing)} 条；可安全重复发布")
    return {
        "version": run_version,
        "runs": runs,
        "verified_scores": len(records),
        "created_scores": created,
        "excluded_dimensions": sum(len(x.get("excluded_dimensions", {})) for x in reviews.values()),
        "agent_reviewed_cases": len(reviews),
        "quality_metrics_do_not_include_unassessable": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=ROOT / "data/evaluation/2026-11-01")
    args = parser.parse_args()
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
        raise ValueError("只允许发布到当前本机 Langfuse")
    items = json.loads((args.folder / "items.json").read_text())
    folder = args.folder / VERSION / "baseline"
    results = [
        json.loads((folder / (item["metadata"]["source_trace_id"] + ".json")).read_text())
        for item in items
    ]
    reviews = json.loads((args.folder / VERSION / "review-overlay.json").read_text())["cases"]
    from langfuse import Langfuse

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        # SDK 的 create_score 也受此开关控制；本脚本只创建分数，不开启任何观察或模型调用。
        tracing_enabled=True,
    )
    try:
        report = publish(client, items, results, reviews)
        (args.folder / VERSION / "publish-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
