"""장기 기억 테스트 (LLM 없음): 미래 기억 차단, 피드백, 최신순 제한, 엔지니어 선호.

실행: uv run pytest tests/test_memory.py -v
"""

from types import SimpleNamespace

import pytest

from memory import store as mem
from tools.context import Ctx
from tools.memory_tools import NO_MEMORY, recall_machine_memory

M = 45
FINDING = {"machine_id": M, "comp": "bearing", "label": "예측 고장", "summary": "진동 상승", "key_evidence": []}


@pytest.fixture(autouse=True)
def fresh_store():
    mem.reset_store()
    yield
    mem.reset_store()


def tool_text(machine_id: int, as_of: str) -> str:
    return recall_machine_memory.func(machine_id, SimpleNamespace(context=Ctx(as_of=as_of)))


def test_future_record_is_hidden():
    mem.save_investigation(M, "2015-11-20 06:00:00", FINDING, "E1120-045")
    assert mem.recall(M, "2015-11-12 06:00:00") == []
    assert tool_text(M, "2015-11-12 06:00:00") == NO_MEMORY


def test_past_record_is_visible():
    mem.save_investigation(M, "2015-11-20 06:00:00", FINDING, "E1120-045")
    records = mem.recall(M, "2015-11-21 06:00:00")
    assert [r["as_of"] for r in records] == ["2015-11-20 06:00:00"]
    assert records[0]["evidence_id"] == "E1120-045"
    assert "2015-11-20: bearing 예측 고장 (E1120-045)" in tool_text(M, "2015-11-21 06:00:00")


def test_same_time_record_is_hidden():
    """같은 시각의 기록은 '이전'이 아니므로 보이지 않는다 (지금 조사 중인 결과가 섞이지 않도록)."""
    mem.save_investigation(M, "2015-11-20 06:00:00", FINDING, "E1120-045")
    assert mem.recall(M, "2015-11-20 06:00:00") == []


def test_feedback_is_recalled():
    mem.save_investigation(M, "2015-11-20 06:00:00", FINDING, "E1120-045")
    mem.save_feedback(M, "2015-11-20 06:00:00", "진동 센서 고정 상태 불량이었음", "eng07")
    records = mem.recall(M, "2015-11-21 06:00:00")
    fb = records[0]["feedback"]
    assert fb[0]["text"] == "진동 센서 고정 상태 불량이었음"
    assert fb[0]["engineer_id"] == "eng07" and fb[0]["as_of"] == "2015-11-20 06:00:00"
    text = tool_text(M, "2015-11-21 06:00:00")
    assert "피드백: 진동 센서 고정 상태 불량이었음" in text


def test_feedback_without_record_creates_one():
    mem.save_feedback(M, "2015-11-20 06:00:00", "현장 확인 결과 이상 없음", "eng01")
    records = mem.recall(M, "2015-11-21 06:00:00")
    assert len(records) == 1 and records[0]["comp"] is None
    assert "현장 확인 결과 이상 없음" in tool_text(M, "2015-11-21 06:00:00")


def test_reinvestigation_keeps_feedback():
    mem.save_investigation(M, "2015-11-20 06:00:00", FINDING, "E1120-045")
    mem.save_feedback(M, "2015-11-20 06:00:00", "볼트 풀림", "eng01")
    mem.save_investigation(M, "2015-11-20 06:00:00", {**FINDING, "summary": "다시 조사"}, "E1120-045")
    record = mem.recall(M, "2015-11-21 06:00:00")[0]
    assert record["summary"] == "다시 조사" and record["feedback"][0]["text"] == "볼트 풀림"


def test_recall_latest_three_only():
    for day in ["2015-11-01", "2015-11-05", "2015-11-10", "2015-11-15", "2015-11-20"]:
        mem.save_investigation(M, f"{day} 06:00:00", FINDING, f"E{day[5:7]}{day[8:]}-045")
    records = mem.recall(M, "2015-11-16 06:00:00")
    assert [r["as_of"][:10] for r in records] == ["2015-11-15", "2015-11-10", "2015-11-05"]


def test_machines_are_separated():
    mem.save_investigation(M, "2015-11-20 06:00:00", FINDING, "E1120-045")
    assert mem.recall(46, "2015-11-21 06:00:00") == []


def test_tool_schema_has_no_as_of_and_validates_input():
    assert set(recall_machine_memory.args) == {"machine_id"}
    assert tool_text(0, "2015-11-21 06:00:00").startswith("입력 오류")


def test_chat_feedback_tool_includes_today():
    """채팅용 get_engineer_feedback은 오늘(같은 시각) 저장한 피드백도 보여준다. 미래 기록은 여전히 숨긴다."""
    from tools.memory_tools import get_engineer_feedback

    rt = SimpleNamespace(context=Ctx(as_of="2015-11-20 06:00:00"))
    mem.save_investigation(M, "2015-11-20 06:00:00", FINDING, "E1120-045")
    mem.save_feedback(M, "2015-11-20 06:00:00", "진동 센서 고정 볼트 풀림", "eng01")
    mem.save_feedback(46, "2015-11-25 06:00:00", "미래 피드백", "eng01")

    assert tool_text(M, "2015-11-20 06:00:00") == NO_MEMORY  # 조사용 recall은 같은 시각 제외 (그대로)
    one = get_engineer_feedback.func(rt, M)
    assert "진동 센서 고정 볼트 풀림" in one and "E1120-045" in one
    everyone = get_engineer_feedback.func(rt, None)
    assert f"[#{M}]" in everyone and "미래 피드백" not in everyone
    assert get_engineer_feedback.func(rt, 46) == NO_MEMORY.join(["[#46 조사 기록·엔지니어 피드백]\n", ""])
    assert set(get_engineer_feedback.args) == {"machine_id"}


def test_engineer_preference_in_planner_message():
    from agents.planner import _user_message

    mem.save_preference("eng01", "병목 설비는 오전 첫 순서로")
    mem.save_preference("eng01", "병목 설비는 오전 첫 순서로")  # 같은 문장은 한 번만
    assert mem.get_preferences("eng01") == ["병목 설비는 오전 첫 순서로"]
    finding = {"machine_id": M, "comp": "bearing", "label": "예측 고장", "prob": 0.9, "evidence_id": "E1120-045",
               "checklist": []}
    msg = _user_message("2015-11-20 06:00:00", [finding], None, "eng01")
    assert msg.startswith("엔지니어 선호: 병목 설비는 오전 첫 순서로")
    assert not _user_message("2015-11-20 06:00:00", [finding], None, "eng02").startswith("엔지니어 선호")
