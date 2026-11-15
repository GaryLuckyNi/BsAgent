"""只修用户确认的三个 Skill 分类，备份原记录，不覆盖工作流正文。"""
import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.graph.nodes.planner_agent import _discover_capabilities
from app.graph.planning.planner import _planner_user_prompt
from app.skills.admin_provider import AdminSkillDefinitionProvider
from app.skills.registry import SkillRegistry

NAMES = ("company_snapshot", "financial_report_analysis", "risk_scan")
FIELDS = {"supported_intents": ["financial_research"],
          "supported_domains": ["a_share_equity_research"],
          "supported_sub_intents": ["general_research"]}

def save(name, data):
    (HERE / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

with httpx.Client(base_url="http://127.0.0.1:8002", trust_env=False, timeout=20) as client:
    if (HERE / "before.json").exists():
        raise SystemExit("已有备份，拒绝隐式重复修改。")
    before = {}
    for name in NAMES:
        response = client.get(f"/api/skills/{name}")
        response.raise_for_status()
        before[name] = response.json()
    save("before.json", before)
    for name, row in before.items():
        assert row["supported_intents"] == ["task_workflow"]
        assert row["supported_domains"] == ["a_share_equity_research"]
        assert row["supported_sub_intents"] == [name]
    after = {}
    for name, row in before.items():
        current = client.get(f"/api/skills/{name}")
        current.raise_for_status()
        assert current.json() == row, "配置已被其他人修改，停止覆盖。"
        response = client.put(f"/api/skills/{name}", json=FIELDS)
        response.raise_for_status()
        after[name] = response.json()
        assert all(after[name][key] == value for key, value in FIELDS.items())
        assert all(after[name][key] == value for key, value in row.items()
                   if key not in {*FIELDS, "updated_at"})
        save("after.json", after)
    registry = SkillRegistry(AdminSkillDefinitionProvider(client=client))
    capabilities = _discover_capabilities(state={}, domain="a_share_equity_research",
        subdomain="general_research", skill_registry=registry, tool_search_service=None)
    prompt = json.loads(_planner_user_prompt(goal="查询公司财务和风险", intent_type="financial_research",
        domain="a_share_equity_research", subdomain="general_research", capabilities=capabilities,
        task_results=(), remaining_iterations=1, planning_constraints=None))
    names = {item["name"] for item in prompt["skills"]}
    assert set(NAMES) <= names
    verification = {"repaired": list(NAMES), "classification": FIELDS,
                    "planner_context_skills": sorted(names), "workflow_summaries_unchanged": True}
    save("verification.json", verification)
    print(json.dumps(verification, ensure_ascii=False))
