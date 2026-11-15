"""只读核对本批独立测试会话的回执与 checkpoint，不修改业务数据库。"""
import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from langgraph.checkpoint.postgres import PostgresSaver

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT/"backend"))
from app.graph.state import build_checkpoint_thread_id
from app.tools.redaction import sanitize_untrusted_details

values=dotenv_values(ROOT/".env.local")
url=make_url(values.get("BS_AGENT_DATABASE_URL") or "postgresql+psycopg://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent")
assert url.host in {"localhost","127.0.0.1"} and url.port==55432 and url.database=="bs_agent"
with psycopg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False),autocommit=True,row_factory=dict_row,
    options="-c default_transaction_read_only=on -c statement_timeout=8000") as conn:
    saver=PostgresSaver(conn)
    for file in sorted((HERE/"results").glob("*.json")):
        result=json.loads(file.read_text())
        with conn.cursor() as cur:
            cur.execute("SELECT run_id, session_id, session_key, trace_id, status, created_at FROM agent_runs WHERE session_id=%s AND tenant_id=%s AND user_id=%s",
                (result["session_id"],"tenant_1","user_1"))
            runs=cur.fetchall()
            if len(runs)!=1:
                print(json.dumps({"case":result["case_id"],"run_count":len(runs)}),flush=True)
                continue
            run=runs[0]
            cur.execute("SELECT producer_done, last_sequence, phase_start_sequence FROM agent_run_executions WHERE run_id=%s",(run["run_id"],))
            execution=cur.fetchone()
            cur.execute("SELECT receipt_id, tool_name, call_id, risk_level, status, result_summary, created_at, completed_at FROM agent_run_tool_receipts WHERE run_id=%s ORDER BY created_at",(run["run_id"],))
            receipts=cur.fetchall()
            cur.execute("SELECT min(created_at) AS started_at,max(created_at) AS ended_at,count(*) AS events FROM agent_run_protocol_events WHERE run_id=%s",(run["run_id"],))
            timings=cur.fetchone()
        tid=build_checkpoint_thread_id(tenant_id="tenant_1",session_key=run["session_key"],request_id=result["request_id"])
        saved=saver.get_tuple({"configurable":{"thread_id":tid,"checkpoint_ns":""}})
        state=saved.checkpoint["channel_values"] if saved else {}
        allowed={"intent","plan","tasks","task_results","task_status","stop_reason","planner_decision","planner_iteration",
            "recovery_policy_version","recovery_source_policy","recovery_items","recovery_batches","recovery_outcomes",
            "tool_call_records","tool_selection_contexts","deferred_tool_calls","tool_dispatch_batches","recovery_dispatches",
            "execution_observations","errors","research_evidence","final_response","dispatch_batches","tool_calls",
            "pending_user_input","capability_observations","skill_workflows","verification","analysis","plan_history"}
        output=dict(case_id=result["case_id"],run=run,execution=execution,timings=timings,receipts=receipts,
            state_keys=sorted(state),state={k:v for k,v in state.items() if k in allowed})
        output=json.loads(json.dumps(output,ensure_ascii=False,default=str))
        output=sanitize_untrusted_details(output,max_depth=30,max_nodes=100000,max_collection_items=10000,max_string_length=100000)
        (HERE/"state").mkdir(exist_ok=True)
        (HERE/"state"/file.name).write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"case":result["case_id"],"status":run["status"],"producer_done":execution["producer_done"],"receipts":[[r["tool_name"],r["status"]] for r in receipts],"stop_reason":state.get("stop_reason")},ensure_ascii=False),flush=True)
