"""定点核实一条历史查询回执；默认只验证，--apply 才修改。"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.conversations.execution import ExecutionControl

RECEIPT = "72ea5e1f31004df5adb9caaa1f054e2d"
RUN = "run_f8d04e85c13ecc33e6a89c59ef651bff41efb4b968a37068f9bd771f08c3"
CURRENT_RUN = "run_cb339307dfa611936d9a8f578f2c04ba86a541550fa66c2f1129aeeab940"
SESSION = "sess_29ae0a72f6be4973aa8e096a2c9f0c49"
EXPECTED_DIGEST = "59a19023762a3d8ab02754a0d979697cf110aab707a656f2f3698f71487d3360"
COMPLETED_AT = "2026-10-24T04:30:57.269631+00:00"


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def main(apply=False):
    values = dotenv_values(Path(__file__).resolve().parents[2] / ".env.local")
    url = make_url(values.get("BS_AGENT_DATABASE_URL") or
                   "postgresql+psycopg://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent")
    assert url.host in {"localhost", "127.0.0.1"} and url.port == 55432
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    if apply:
        with psycopg.connect(dsn, row_factory=dict_row,
                             options="-c lock_timeout=3000 -c statement_timeout=5000") as db:
            session = db.execute(
                "SELECT * FROM agent_sessions WHERE session_key=%s FOR UPDATE", (SESSION,),
            ).fetchone()
            assert session and session["tenant_id"] == "tenant_1" and session["user_id"] == "user_1"
            assert session["active_run_id"] is None
            run = db.execute("SELECT * FROM agent_runs WHERE run_id=%s FOR UPDATE", (RUN,)).fetchone()
            execution = db.execute(
                "SELECT * FROM agent_run_executions WHERE run_id=%s FOR UPDATE", (RUN,),
            ).fetchone()
            assert run and run["session_key"] == SESSION and run["status"] == "completed"
            assert run["tenant_id"] == "tenant_1" and run["user_id"] == "user_1"
            assert execution and execution["producer_done"]
            row = db.execute(
                "SELECT * FROM agent_run_tool_receipts WHERE receipt_id=%s FOR UPDATE", (RECEIPT,),
            ).fetchone()
            assert row and row["run_id"] == RUN and row["tool_name"] == "mcp.tushare.anns_d"
            assert row["call_id"] == "call_b70c751a6a1728f120c033a17a1fc12b"
            assert row["risk_level"] == "medium" and row["status"] == "unknown"
            assert row["completed_at"].isoformat() == COMPLETED_AT
            original = row["result_summary"]
            assert digest(original) == EXPECTED_DIGEST and "reconciliation" not in original
            assert original["status"] == "failed" and original["execution_state"] == "uncertain"
            assert original["error"]["code"] == "mcp_tool_execution_failed"
            summary = {**original, "reconciliation": {
                "action": "settle_verified_read_only_failure",
                "resolution": "verified_no_business_write_effects",
                "authority": "user_requested_historical_receipt_repair",
                "verified_at": datetime.now(UTC).isoformat(),
                "previous_receipt_status": "unknown",
                "original_summary_sha256": EXPECTED_DIGEST,
                "source": "https://tushare.pro/document/2?doc_id=176",
                "reason": "anns_d 为公告数据查询；原运行及生产者已结束，无需挂起业务写入副作用。",
                "permission_denial_independently_confirmed": False,
                "remote_execution_replayed": False,
            }}
            changed = db.execute(
                "UPDATE agent_run_tool_receipts SET status='settled',result_summary=%s "
                "WHERE receipt_id=%s AND run_id=%s AND status='unknown'",
                (Jsonb(summary), RECEIPT, RUN),
            ).rowcount
            assert changed == 1
        print(json.dumps({"updated_rows": changed, "receipt_id": RECEIPT}))

    # 独立新连接验证；不更新完成时间、回执风险或原始失败结果。
    with psycopg.connect(dsn, row_factory=dict_row,
                         options="-c default_transaction_read_only=on -c statement_timeout=5000") as db:
        row = db.execute("SELECT * FROM agent_run_tool_receipts WHERE receipt_id=%s", (RECEIPT,)).fetchone()
        assert row["status"] == "settled" and row["risk_level"] == "medium"
        assert row["completed_at"].isoformat() == COMPLETED_AT
        summary = dict(row["result_summary"])
        audit = summary.pop("reconciliation")
        assert digest(summary) == EXPECTED_DIGEST
        assert audit["original_summary_sha256"] == EXPECTED_DIGEST
        count = db.execute(
            "SELECT count(*) AS n FROM agent_run_tool_receipts t JOIN agent_runs r USING(run_id) "
            "WHERE r.session_key=%s AND r.tenant_id='tenant_1' "
            "AND t.risk_level IN ('medium','high') AND t.status IN ('unknown','running')", (SESSION,),
        ).fetchone()["n"]
        assert count == 0
        print(json.dumps({"receipt_status": row["status"], "result_status": summary["status"],
                          "original_summary_unchanged": True, "blocking_receipts": count,
                          "verified_at": audit["verified_at"]}))
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    try:
        with Session(engine) as db:
            control = ExecutionControl(session_factory=None, run_id=CURRENT_RUN, owner="read-only-audit")
            for risk in ("low", "medium", "high"):
                control.ensure_tool_effects_resolved(risk_level=risk, db=db)
                print(json.dumps({"risk": risk, "guard_result": "allowed"}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args().apply)
