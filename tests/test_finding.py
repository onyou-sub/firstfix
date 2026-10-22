"""Finding 길이 제한·다시 쓰기·말줄임·사실 확인·옛 파일 호환 (LLM 없음)."""

import pytest
from pydantic import ValidationError

from agents.investigator import check_grounding, finalize_finding, ground_finding, length_problems
from schemas import LIMITS, Finding, finding_compat


def draft(**over) -> dict:
    d = {
        "machine_id": 45, "comp": "bearing", "label": "예측 고장",
        "action_summary": "하우징 온도·윤활 확인 후 이상 시 베어링 교체",
        "anomaly_summary": "진동 72h 상승, 24h 평균 50.4(평소 대비 +25%)",
        "hypotheses": [{"cause": "윤활 부족", "evidence": "진동 24h 평균 50.4(평소 대비 +25%)"}],
        "steps": [{"action": "설비 정지·잠금 확인", "criterion": "잠금 태그 부착", "if_abnormal": "작업 중지"},
                  {"action": "하우징 온도 측정", "criterion": "평소 수준과 비교", "if_abnormal": "윤활 보충 후 재측정"},
                  {"action": "베어링 소음 확인", "criterion": "이음 없음", "if_abnormal": "베어링 교체"}],
        "field_tips": ["측정 전 10분 공회전"], "est_minutes": 90, "parts_tools": ["베어링", "온도계"],
        "summary": "진동이 72시간째 오르고 있습니다. 윤활부터 확인합니다.",
        "key_evidence": ["진동 24h 평균 50.4"], "checklist": ["점검 전 설비 정지 절차 확인", "윤활 상태 확인"],
        "manual_source": "",
    }
    return {**d, **over}


def test_valid_draft_passes_without_rewrite():
    called = []
    f, log = finalize_finding(draft(), rewrite=lambda d, p: called.append(p) or d)
    assert isinstance(f, Finding) and log["problems"] == [] and not log["rewritten"] and not called


def test_limits_enforced_by_model():
    with pytest.raises(ValidationError):
        Finding.model_validate(draft(action_summary="가" * (LIMITS["action_summary"] + 1)))
    with pytest.raises(ValidationError):
        Finding.model_validate(draft(summary="한 문장. 두 문장. 세 문장."))
    with pytest.raises(ValidationError):
        Finding.model_validate(draft(steps=draft()["steps"][:2]))


def test_too_long_asks_rewrite_once_then_ok():
    long = draft(action_summary="가" * 60)
    calls = []

    def rewrite(d, problems):
        calls.append(problems)
        return {**d, "action_summary": "윤활 확인 후 베어링 교체"}

    f, log = finalize_finding(long, rewrite=rewrite)
    assert len(calls) == 1 and any("action_summary" in p for p in calls[0])
    assert log["rewritten"] and log["after_rewrite"] == [] and not log["ellipsized"]
    assert f.action_summary == "윤활 확인 후 베어링 교체"  # 잘라낸 것이 아니라 다시 쓴 문장


def test_still_too_long_after_rewrite_gets_ellipsis():
    long = draft(action_summary="가" * 60, summary="하나. 둘. 셋.",
                 hypotheses=[{"cause": "원인" * 20, "evidence": "e"}] * 5, steps=draft()["steps"][:1])
    f, log = finalize_finding(long, rewrite=lambda d, p: d)  # 다시 써도 그대로
    assert log["rewritten"] and log["after_rewrite"] and log["ellipsized"]
    assert len(f.action_summary) == LIMITS["action_summary"] and f.action_summary.endswith("…")
    assert len(f.hypotheses) == LIMITS["hypotheses"] and all(len(h.cause) <= 15 for h in f.hypotheses)
    assert len(f.steps) >= LIMITS["steps_min"]  # 모자란 단계는 checklist로 채움
    assert f.summary == "하나. 둘."
    assert length_problems(f.model_dump()) == []


def test_rewrite_failure_falls_back_to_ellipsis():
    def boom(d, p):
        raise RuntimeError("API down")

    f, log = finalize_finding(draft(anomaly_summary="나" * 100), rewrite=boom)
    assert log["ellipsized"] and "rewrite_error" in log and len(f.anomaly_summary) == LIMITS["anomaly_summary"]


def test_old_file_compat_uses_first_non_safety_step():
    old = {"machine_id": 1, "comp": "motor", "label": "예측 고장", "summary": "전압이 높습니다.",
           "key_evidence": [], "manual_source": "",
           "checklist": ["점검 전 설비 정지 절차 확인", "단자 체결 상태 확인"]}
    assert finding_compat(old)["action_summary"] == "단자 체결 상태 확인"
    with_steps = {**old, "steps": [{"action": "설비 정지·잠금 확인"}, {"action": "전압 측정"}]}
    assert finding_compat(with_steps)["action_summary"] == "전압 측정"
    assert finding_compat({**old, "action_summary": "그대로"})["action_summary"] == "그대로"
    long = finding_compat({**old, "checklist": ["가" * 80]})["action_summary"]
    assert len(long) == LIMITS["action_summary"] and long.endswith("…")
    assert finding_compat(old)["steps"] == [] and finding_compat(old)["hypotheses"] == []


# ---------------------------------------------------------------- 사실 확인 (가짜 tool_calls)
TOOL_CALLS = [
    {"name": "recall_machine_memory", "input": {"machine_id": 45},
     "output": "2015-09-28 조사: bearing 윤활 부족 (WO-0928-045 완료)"},
    {"name": "get_sensor_trend", "input": {"machine_id": 45, "sensor": "vibration", "hours": 72},
     "output": "[설비 45 진동 최근 72시간]\n- 24시간 평균: 50.37 (1년 평균 40.4 대비 +24.7%)"},
    {"name": "search_field_notes", "input": {"machine_id": 45}, "output": "10/01 작업자 보고: 이음 발생"},
    {"name": "search_manual", "input": {"query": "진동"}, "output": "주의: 24시간 평균이 1년 평균 대비 25% 이상"},
]


def grounded(**over) -> dict:
    return draft(**{"anomaly_summary": "진동 72h 상승, 24h 평균 50.4(평소 대비 +24.7%)",
                    "hypotheses": [{"cause": "윤활 부족", "evidence": "진동 24h 평균 50.4, 평소 대비 +24.7%"}],
                    "field_tips": ["10/01 이음 보고 위치부터 확인", "2015-09-28 WO-0928-045 조치 참고"], **over})


def test_grounding_passes_when_numbers_dates_wo_in_tool_outputs():
    # 50.4 <- 50.37 (소수 첫째 자리 반올림), 25%는 매뉴얼, 72·24는 도구 출력, 날짜·WO는 기억·현장 기록
    assert check_grounding(grounded(), TOOL_CALLS) == []
    assert check_grounding(grounded(anomaly_summary="진동 24h 평균 50.4(평소 대비 25%)"), TOOL_CALLS) == []


def test_grounding_flags_made_up_numbers():
    p = check_grounding(grounded(anomaly_summary="진동 24h 평균 61.2",
                                 hypotheses=[{"cause": "윤활 부족", "evidence": "진동 평균 50.4, 온도 83도 상승"}]),
                        TOOL_CALLS)
    assert any(x.startswith("anomaly_summary") and "61.2" in x for x in p)
    assert any(x.startswith("hypotheses[0]") and "83" in x and "50.4" not in x for x in p)


def test_grounding_flags_dates_and_wo_missing_from_memory_tools():
    tips = ["10/05 교체 이력 확인", "WO-1001-045 참고", "2015-10-01 보고 확인", "측정 전 10분 공회전"]
    p = check_grounding(grounded(field_tips=tips), TOOL_CALLS)
    assert any("field_tips[0]" in x and "10/05" in x for x in p)
    assert any("field_tips[1]" in x and "WO-1001-045" in x for x in p)
    assert not any("field_tips[2]" in x for x in p)  # 2015-10-01 == 현장 기록의 10/01
    assert not any("field_tips[3]" in x for x in p)  # 날짜·WO 없음 -> 검사 안 함
    # 날짜가 기억·현장 기록이 아닌 다른 도구 출력에만 있으면 근거 아님
    only_trend = [c for c in TOOL_CALLS if c["name"] not in ("search_field_notes", "recall_machine_memory")]
    assert any("field_tips[0]" in x for x in check_grounding(grounded(), only_trend))


def test_grounding_flags_empty_or_short_evidence():
    p = check_grounding(grounded(hypotheses=[{"cause": "윤활 부족", "evidence": ""},
                                             {"cause": "정렬 불량", "evidence": "진동 상승"}]), TOOL_CALLS)
    assert any("hypotheses[0]" in x for x in p) and any("hypotheses[1]" in x for x in p)


def test_grounding_flags_non_safety_first_step():
    steps = draft()["steps"]
    assert any(x.startswith("steps[0]") for x in check_grounding(grounded(steps=steps[1:]), TOOL_CALLS))


def test_ground_finding_inserts_safety_step_without_rewrite():
    called = []
    steps = draft()["steps"][1:] + [{"action": f"점검 {i}", "criterion": "-", "if_abnormal": "-"} for i in range(4)]
    d, log = ground_finding(grounded(steps=steps), TOOL_CALLS, rewrite=lambda d, p, t: called.append(p) or d)
    assert log["safety_inserted"] and not called and log["problems"] == []
    assert d["steps"][0]["action"] == "설비 정지·잠금 확인" and len(d["steps"]) == LIMITS["steps"]


def test_ground_finding_rewrite_once_fixes_problems():
    bad = grounded(hypotheses=[{"cause": "윤활 부족", "evidence": "진동 평균 99.9"}])
    calls = []

    def rewrite(d, problems, tool_calls):
        calls.append(problems)
        return {**d, "hypotheses": [{"cause": "윤활 부족", "evidence": "진동 24h 평균 50.4(+24.7%)"}]}

    d, log = ground_finding(bad, TOOL_CALLS, rewrite=rewrite)
    assert len(calls) == 1 and any("99.9" in p for p in calls[0])
    assert log["rewritten"] and log["after_rewrite"] == [] and log["removed"] == []
    assert d["hypotheses"][0]["evidence"] == "진동 24h 평균 50.4(+24.7%)"


def test_ground_finding_removes_still_ungrounded_items():
    bad = grounded(anomaly_summary="진동 평균 77.7",
                   hypotheses=[{"cause": "윤활 부족", "evidence": "진동 24h 평균 50.4(+24.7%)"},
                               {"cause": "정렬 불량", "evidence": "축 편심 0.35mm 측정"},
                               {"cause": "센서 불량", "evidence": "짧음"}],
                   field_tips=["10/01 이음 보고 위치부터 확인", "WO-9999-001 조치 참고"])
    d, log = ground_finding(bad, TOOL_CALLS, rewrite=lambda d, p, t: d)  # 고쳐 써도 그대로
    assert log["rewritten"] and log["after_rewrite"]
    assert [h["cause"] for h in d["hypotheses"]] == ["윤활 부족"]
    assert d["field_tips"] == ["10/01 이음 보고 위치부터 확인"]
    assert {r["field"] for r in log["removed"]} == {"hypotheses", "field_tips"} and len(log["removed"]) == 3
    assert any("anomaly_summary" in x for x in log["unresolved"])  # 이상 요약은 빼지 않고 기록
    f, _ = finalize_finding(d, rewrite=lambda d, p: d)
    assert isinstance(f, Finding)


def test_ground_finding_rewrite_failure_falls_back_to_removal():
    def boom(d, p, t):
        raise RuntimeError("API down")

    d, log = ground_finding(grounded(field_tips=["WO-9999-001 조치 참고"]), TOOL_CALLS, rewrite=boom)
    assert "rewrite_error" in log and d["field_tips"] == []
