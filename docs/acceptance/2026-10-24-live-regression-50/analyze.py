"""从真实结果和 Langfuse 导出生成逐条审阅底稿，不使用额外评判模型。"""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path

HERE=Path(__file__).resolve().parent


def decode(value):
    if isinstance(value,str):
        try:
            return decode(json.loads(value))
        except (ValueError,RecursionError):
            return value
    if isinstance(value,dict):
        return {k:decode(v) for k,v in value.items()}
    if isinstance(value,list):
        return [decode(v) for v in value]
    return value


def dump(value):
    return json.dumps(value,ensure_ascii=False,separators=(",",":"))


def main():
    summaries=[]
    for file in sorted((HERE/"results").glob("*.json")):
        result=json.loads(file.read_text())
        opath=HERE/"observations"/file.name
        observations=json.loads(opath.read_text()) if opath.exists() else {}
        rows=sorted(observations.get("observations",[]),key=lambda r:r["startTime"])
        byid={r["id"]:r for r in rows}
        parsed={r["id"]:{"input":decode(r.get("input")),"output":decode(r.get("output"))} for r in rows}
        gens=[]
        for r in rows:
            if r["type"]!="GENERATION":
                continue
            io=parsed[r["id"]]
            isjudge="Evaluate answer quality, groundedness" in dump(io["input"])
            parent=byid.get(r.get("parentObservationId"),{}).get("name")
            gens.append(dict(id=r["id"],name=r["name"],parent=parent,judge=isjudge,latency=r.get("latency"),
                model=r.get("model"),usage=r.get("usageDetails"),cost=r.get("costDetails"),
                input_chars=len(dump(io["input"])),output_chars=len(dump(io["output"])),
                input=io["input"],output=io["output"]))
        events=[e for e in result["events"] if isinstance(e,dict)]
        final=next((e["response"] for e in reversed(events) if e.get("type")=="final"),{})
        assistants=[m for m in result.get("messages",{}).get("items",[]) if m.get("role")=="assistant"]
        answer=final.get("content") or (assistants[-1].get("content") if assistants else None)
        planners=[dict(id=r["id"],input=parsed[r["id"]]["input"],output=parsed[r["id"]]["output"])
            for r in rows if r["name"]=="planner_agent"]
        routers=[parsed[r["id"]]["output"] for r in rows if r["name"]=="intent_router"]
        dispatch=[parsed[r["id"]]["output"] for r in rows if r["name"]=="dispatch_task_scheduler"]
        roots=[r for r in rows if r["name"]=="bs-agent.run"]
        tools=[dict(id=r["id"],name=r["name"],type=r["type"],latency=r.get("latency"),level=r.get("level"),
            **parsed[r["id"]]) for r in rows if r["type"]=="TOOL" or any(s in r["name"] for s in ["mcp.","web_search","recovery","receipt"]) ]
        brief=dict(case_id=result["case_id"],category=result["category"],question=result["question"],status=result["status"],
            response_type=final.get("response_type"),business_trace_id=observations.get("business_trace_id"),
            langfuse_trace_id=observations.get("langfuse_trace_id"),root_latency=roots[0].get("latency") if roots else None,
            elapsed_seconds=result.get("elapsed_seconds"),collector_replay=bool(result.get("replay_started_at")),
            observation_count=len(rows),generation_count=len(gens),foreground_generations=sum(not g["judge"] for g in gens),
            judge_generations=sum(g["judge"] for g in gens),planner_node_count=len(planners),
            generation_parents=dict(Counter(g["parent"] for g in gens if not g["judge"])),
            dispatch_reasons=[d.get("reason") for d in dispatch if isinstance(d,dict)],
            response_chars=len(answer or ""),transport_error=result.get("transport_error"),
            interruptions=[e for e in events if "input" in e.get("type","") or "approval" in e.get("type","")],
            error_observations=[dict(id=r["id"],name=r["name"],statusMessage=r.get("statusMessage")) for r in rows if r.get("level")=="ERROR"],
            token_total=sum((g["usage"] or {}).get("total",0) for g in gens),
            foreground_token_total=sum((g["usage"] or {}).get("total",0) for g in gens if not g["judge"]))
        detail=dict(**brief,answer=answer,router_outputs=routers,planner_io=planners,dispatch_outputs=dispatch,generations=gens,tools=tools)
        (HERE/"review").mkdir(exist_ok=True)
        (HERE/"review"/file.name).write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding="utf-8")
        summaries.append(brief)
    (HERE/"summary.json").write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding="utf-8")
    for s in summaries:
        print(dump({k:s[k] for k in ("case_id","status","response_type","observation_count","foreground_generations","root_latency")}))


if __name__=="__main__":
    main()
