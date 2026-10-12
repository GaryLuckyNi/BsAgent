import os

from sqlalchemy.orm import Session

from app.database import SessionLocal, create_tables
from app.models import SkillProfileRecord


def seed_default_skills(
    session: Session,
    *,
    mcp_server_name: str = "local",
) -> int:
    created = 0
    for payload in _default_skill_profiles(mcp_server_name=mcp_server_name):
        if session.get(SkillProfileRecord, payload["name"]) is not None:
            continue
        session.add(SkillProfileRecord(**payload))
        created += 1
    session.commit()
    return created


def _default_skill_profiles(*, mcp_server_name: str) -> list[dict]:
    prefix = f"mcp.{mcp_server_name}.stock"
    common = {
        "version": "1.0.0",
        "supported_intents": ["task_workflow"],
        "supported_domains": ["a_share_equity_research"],
        "risk_level": "medium",
        "enabled": True,
    }
    return [
        {
            **common,
            "name": "company_snapshot",
            "summary": _workflow_summary(
                "生成 A 股公司基础资料、行情、公告与知识证据速览",
                [
                    (f"{prefix}.resolve_company", ["symbol", "name"]),
                    (f"{prefix}.company_profile", ["name", "industry", "main_business"]),
                    (f"{prefix}.market_snapshot", ["price"]),
                    (f"{prefix}.announcement_search", []),
                    ("rag_search", []),
                ],
            ),
            "supported_sub_intents": ["company_snapshot"],
        },
        {
            **common,
            "name": "financial_report_analysis",
            "summary": _workflow_summary(
                "分析 A 股公司指定报告期的财务指标与公告证据",
                [
                    (f"{prefix}.resolve_company", ["symbol", "name"]),
                    (f"{prefix}.financial_metrics", ["revenue", "net_profit_parent"]),
                    (f"{prefix}.announcement_search", []),
                    ("rag_search", []),
                ],
            ),
            "supported_sub_intents": ["financial_report_analysis"],
        },
        {
            **common,
            "name": "risk_scan",
            "summary": _workflow_summary(
                "扫描 A 股公司公告与知识证据",
                [
                    (f"{prefix}.resolve_company", ["symbol", "name"]),
                    (f"{prefix}.announcement_search", ["announcements"]),
                    ("rag_search", []),
                ],
            ),
            "supported_sub_intents": ["risk_scan"],
        },
    ]


def _workflow_summary(title: str, steps: list[tuple[str, list[str]]]) -> str:
    lines = [title + "。"]
    for index, (tool, required_fields) in enumerate(steps, 1):
        line = f"第 {index} 步：调用工具 `{tool}`，根据用户问题和前序步骤结果填写参数。"
        if required_fields:
            line += "必须返回字段：" + "、".join(required_fields) + "；缺失则停止并说明原因。"
        else:
            line += "结果为空时明确标注缺失，允许继续后续步骤。"
        lines.append(line)
    lines.append("最后仅依据实际结果用中文整理报告，列出数据来源和缺失项，不得补造数据。")
    return "\n".join(lines)


def main() -> None:
    create_tables()
    with SessionLocal() as session:
        created = seed_default_skills(
            session,
            mcp_server_name=os.getenv("ADMIN_MCP_SERVER_NAME", "local"),
        )
    print(f"默认投研 Skill 初始化完成，新增 {created} 条。")


if __name__ == "__main__":
    main()
