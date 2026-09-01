import pytest

from app.memory.candidates import propose_explicit_preference_candidates


def _candidates(message: str):
    return propose_explicit_preference_candidates(
        user_message=message,
        tenant_id="tenant-1",
        user_id="user-1",
    )


def test_proposes_explicit_language_preference_with_stable_identity():
    first = _candidates("请记住：以后回答我时使用英文。")
    second = _candidates("请记住：以后回答我时使用英文。")

    assert len(first) == 1
    assert first[0].memory_type == "user_preference"
    assert first[0].memory_key == "language"
    assert first[0].content == "以后回答我时使用英文"
    assert first[0].candidate_id == second[0].candidate_id
    assert first[0].memory_key == second[0].memory_key


def test_merges_duplicate_explicit_preferences():
    candidates = _candidates("记住：回答简洁；请记住回答简洁。")

    assert len(candidates) == 1
    assert candidates[0].memory_key == "response_detail"


def test_does_not_propose_candidate_for_general_knowledge_question():
    assert _candidates("什么是资产负债表？") == []


@pytest.mark.parametrize(
    "message",
    [
        "你的回答偏好是什么？",
        "可以记住什么？",
        "请解释什么是回答偏好。",
    ],
)
def test_does_not_propose_candidate_for_preference_questions_or_explanations(message):
    assert _candidates(message) == []


@pytest.mark.parametrize(
    ("message", "memory_key"),
    [
        ("请记住我的回答偏好是简洁", "response_detail"),
        ("以后请用中文回答", "language"),
        ("我更关注现金流", "focus_domain"),
    ],
)
def test_proposes_candidate_for_explicit_preference_statements(message, memory_key):
    candidates = _candidates(message)

    assert len(candidates) == 1
    assert candidates[0].memory_key == memory_key


def test_marks_email_and_phone_preferences_pending_approval():
    email = _candidates("记住：我的联系邮箱是 alice@example.com。")
    phone = _candidates("请记住：我的联系电话是 13800138000。")

    assert email[0].sensitivity == "high"
    assert email[0].approval_status == "pending"
    assert phone[0].sensitivity == "high"
    assert phone[0].approval_status == "pending"


def test_rejects_credentials_instead_of_proposing_candidate():
    assert _candidates("请记住：我的 token 是 sk-abcdefghijklmnopqrstuvwxyz") == []
    assert _candidates("请记住：密码是 correct-horse-battery-staple") == []
    assert _candidates("请记住：DSN 是 postgresql://user:pass@db.example.com/app") == []
