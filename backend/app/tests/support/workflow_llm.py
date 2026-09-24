import json

from app.llm.schemas import LlmCompletionResult


class WorkflowTestLlm:
    async def complete(self, **kwargs):
        system_prompt = kwargs["system_prompt"]
        user_prompt = json.loads(kwargs["user_prompt"])
        if "投研任务理解器" in system_prompt:
            taxonomy = user_prompt["allowed_taxonomy"]
            message = str(user_prompt["message"]).strip()
            if not taxonomy or message in {"你好", "您好", "hi", "hello"}:
                payload = {
                    "intent_type": None,
                    "domain": None,
                    "subdomain": None,
                    "goal": None,
                    "confidence": 1.0,
                }
            else:
                profile = next(
                    (
                        item
                        for item in taxonomy
                        if item["subdomain"] == "company_snapshot"
                    ),
                    taxonomy[0],
                )
                payload = {
                    "intent_type": profile["intent_type"],
                    "domain": profile["domain"],
                    "subdomain": profile["subdomain"],
                    "goal": user_prompt["message"],
                    "confidence": 1.0,
                }
            content = json.dumps(payload, ensure_ascii=False)
        elif "计划反馈决策器" in system_prompt:
            content = json.dumps({"action": "finish", "reason": "完成本次研究，保留证据限制"}, ensure_ascii=False)
        elif "投研任务 Planner" in system_prompt:
            content = json.dumps(
                {
                    "tasks": [
                        {
                            "id": "answer",
                            "task_goal": user_prompt["goal"],
                            "depends_on": [],
                            "skill_name": None,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        elif "投研任务执行器" in system_prompt:
            content = "测试环境未提供外部投研数据。"
        elif "投研报告合成器" in system_prompt:
            content = "## 分析结果\n\n测试环境未提供外部投研数据。"
        else:
            content = "测试响应"
        return LlmCompletionResult(content=content, model="test", provider="test")

    async def stream_complete(self, **_kwargs):
        yield "正在处理投研任务。"
