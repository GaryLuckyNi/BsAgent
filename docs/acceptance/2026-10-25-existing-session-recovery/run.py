"""原会话真实验收；不核销旧回执，不自动确认，不隐式重复提交。"""

import argparse
import asyncio
import importlib.util
import json
import sys
from hashlib import sha256
from pathlib import Path

import httpx
import psycopg
from dotenv import dotenv_values
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.graph.state import build_checkpoint_thread_id

spec = importlib.util.spec_from_file_location(
    "collector", HERE.parent / "2026-10-24-live-regression-50/run_cases.py"
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
collector.HERE = HERE
SESSION = "session_5f172f3f"
REQUEST = "unknown-recovery-20261025-original-001"
HEADERS = {"X-Tenant-ID": "tenant_1", "X-User-ID": "user_1"}
RESULT = HERE / "results/001.json"


def audit(label):
    values = dotenv_values(ROOT / ".env.local")
    url = make_url(values.get("BS_AGENT_DATABASE_URL") or
                   "postgresql+psycopg://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent")
    assert url.host in {"localhost", "127.0.0.1"} and url.port == 55432
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True, row_factory=dict_row,
        options="-c default_transaction_read_only=on -c statement_timeout=8000",
    ) as conn:
        old = conn.execute(
            "SELECT status,result_summary FROM agent_run_tool_receipts WHERE receipt_id=%s",
            ("72ea5e1f31004df5adb9caaa1f054e2d",),
        ).fetchone()
        output = {"old_receipt_status": old["status"], "old_receipt_digest": sha256(
            json.dumps(old["result_summary"], sort_keys=True).encode()
        ).hexdigest()}
        if RESULT.exists():
            result = json.loads(RESULT.read_text())
            run_id = result.get("run_id")
            run = conn.execute(
                "SELECT r.status,r.session_key,e.producer_done FROM agent_runs r "
                "JOIN agent_run_executions e USING(run_id) WHERE r.run_id=%s", (run_id,),
            ).fetchone()
            if run:
                output["run"] = run
                output["receipts"] = conn.execute(
                    "SELECT tool_name,status FROM agent_run_tool_receipts WHERE run_id=%s", (run_id,),
                ).fetchall()
                output["events"] = conn.execute(
                    "SELECT event,data->>%s AS type,count(*) AS n FROM agent_run_protocol_events "
                    "WHERE run_id=%s GROUP BY 1,2", ("type", run_id),
                ).fetchall()
                thread = build_checkpoint_thread_id(
                    tenant_id="tenant_1", session_key=run["session_key"], request_id=REQUEST,
                )
                saved = PostgresSaver(conn).get_tuple({"configurable": {
                    "thread_id": thread, "checkpoint_ns": "",
                }})
                state = saved.checkpoint["channel_values"] if saved else {}
                output["checkpoint"] = {k: state.get(k) for k in (
                    "recovery_items", "recovery_batches", "recovery_outcomes", "pending_user_input",
                )}
        output = json.loads(json.dumps(output, ensure_ascii=False, default=str))
        collector.save(HERE / f"audit-{label}.json", output)
        print(json.dumps({k: v for k, v in output.items() if k != "checkpoint"}, ensure_ascii=False))


def submit():
    if RESULT.exists():
        raise SystemExit("已有提交记录，拒绝隐式重发；请只采集现有请求。")
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(30, read=90)) as client:
        active = client.get(f"http://127.0.0.1:8000/api/sessions/{SESSION}/runs/active", headers=HEADERS)
        active.raise_for_status()
        if active.json().get("run"):
            raise SystemExit("原会话有活动请求；不打断、不重复提交。")
        audit("before")
        result = {"case_id": "001", "session_id": SESSION, "request_id": REQUEST,
                  "question": "汇金科技为什么能涨停", "started_at": collector.now(), "events": []}
        collector.save(RESULT, result)
        with client.stream(
            "POST", f"http://127.0.0.1:8000/api/sessions/{SESSION}/messages/protocol-stream",
            headers=HEADERS, json={"input": {"messages": [{"type": "human", "content": result["question"]}],
                                               "request_id": REQUEST}},
        ) as response:
            result["http_status"] = response.status_code
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:])
                result["events"].append(event)
                if isinstance(event, dict):
                    for key in ("run_id", "trace_id"):
                        if event.get(key):
                            result["business_trace_id" if key == "trace_id" else key] = event[key]
                collector.save(RESULT, result)
        messages = client.get(f"http://127.0.0.1:8000/api/sessions/{SESSION}/messages", headers=HEADERS)
        messages.raise_for_status()
        result["messages"] = messages.json()
        result["ended_at"] = collector.now()
        collector.save(RESULT, result)
        print(json.dumps({"run_id": result.get("run_id"), "trace_id": result.get("business_trace_id"),
                          "events": [e.get("type") for e in result["events"] if isinstance(e, dict)]}))
        audit("after")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("submit", "collect", "audit"))
    mode = parser.parse_args().mode
    if mode == "submit":
        submit()
    elif mode == "collect":
        asyncio.run(collector.collect())
    else:
        audit("after")
