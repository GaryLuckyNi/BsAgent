from app.intent.schemas import IntentSignal


HIGH_RISK_MARKERS = ("删除", "清空", "移除", "写入", "发送", "提交", "修改", "更新")
QUESTION_MARKERS = ("什么", "怎么", "如何", "为什么", "是否", "？", "?")
CONVERSATION_MARKERS = ("你好", "您好", "早上好", "谢谢", "辛苦了")


class RulePreRouter:
    def analyze(self, message: str) -> IntentSignal:
        stripped = message.strip()
        reason_codes: list[str] = []
        if not stripped:
            return IntentSignal(
                hard_intent="clarification_needed",
                needs_clarification=True,
                reason_codes=["empty_message"],
            )

        if any(marker in stripped for marker in CONVERSATION_MARKERS) and len(stripped) <= 12:
            return IntentSignal(
                risk_floor="low",
                hard_intent="conversation",
                reason_codes=["rule_conversation"],
            )

        risk_floor = "medium"
        hard_intent = None
        if any(marker in stripped for marker in HIGH_RISK_MARKERS):
            risk_floor = "high"
            reason_codes.append("rule_high_risk_action")
            if not any(marker in stripped for marker in QUESTION_MARKERS):
                hard_intent = "tool_action"

        if any(marker in stripped for marker in QUESTION_MARKERS):
            reason_codes.append("rule_question_like")

        return IntentSignal(
            risk_floor=risk_floor,
            hard_intent=hard_intent,
            reason_codes=reason_codes,
        )
